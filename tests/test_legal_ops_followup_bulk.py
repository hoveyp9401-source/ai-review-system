from datetime import datetime, time, timezone
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    CaseFollowupPolicy,
)
from app.legal_ops.followup_bulk import (
    BulkFollowupChange,
    BulkFollowupFilter,
    preview_bulk_followup_change,
)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, binding, cases, policies):
        self.binding = binding
        self.cases = cases
        self.policies = policies

    async def scalars(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_identity_bindings" in sql:
            return _Rows((self.binding,))
        if "agent2_case_followup_policies" in sql:
            return _Rows(self.policies)
        if "agent2_case_followup_tasks" in sql:
            return _Rows(())
        if "agent2_cases" in sql:
            return _Rows(self.cases)
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_bulk_preview_preserves_manual_override_unless_force_is_explicit():
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    user_id = "user-1"
    case_id = uuid4()
    binding = Agent2IdentityBinding(
        binding_id=uuid4(), tenant_id="tenant-a", company_id="company",
        department_id="legal", team_id="team", user_id=user_id,
        dingtalk_user_id="ding-user-1", display_name="庞浩", role_ids=["lawyer"],
        permission_scope_json={"allowed_case_ids": [str(case_id)]}, active=True,
        created_at=now, updated_at=now,
    )
    case = Agent2Case(
        case_id=case_id, tenant_id="tenant-a", company_id="company",
        department_id="legal", team_id="team", external_case_id="P-1",
        case_number="(2026)苏01民初1号", case_name="南京工程款案",
        case_type="plaintiff", status="open", owner_user_id=user_id,
        source_type="real_source_sandbox", source_id="source-1",
        source_json={"major_stage": "诉讼中", "risk_level": "high"},
        version=2, created_at=now, updated_at=now,
    )
    policy = CaseFollowupPolicy(
        policy_id=uuid4(), tenant_id="tenant-a", case_id=case_id,
        assigned_user_id=user_id, enabled=True,
        policy_source="case_manual_override", cadence_type="daily",
        cadence_days=None, custom_interval_json={}, timezone="Asia/Shanghai",
        business_days_only=False, allowed_start_time=time(9), allowed_end_time=time(18),
        event_triggers_enabled=True, hearing_reminders_enabled=True,
        stage_transition_enabled=True, node_transition_enabled=True,
        max_unanswered_reminders=1, version=3, created_at=now, updated_at=now,
    )
    session = _Session(binding, (case,), (policy,))

    preserved = await preview_bulk_followup_change(
        session, tenant_id="tenant-a",
        filters=BulkFollowupFilter(case_type="plaintiff", stage="诉讼中"),
        change=BulkFollowupChange(cadence_type="weekly", enabled=True),
    )
    forced = await preview_bulk_followup_change(
        session, tenant_id="tenant-a",
        filters=BulkFollowupFilter(case_type="plaintiff", stage="诉讼中"),
        change=BulkFollowupChange(
            cadence_type="weekly", enabled=True, force_manual_override=True
        ),
    )

    assert preserved["matched_count"] == 1
    assert preserved["change_count"] == 0
    assert preserved["items"][0]["skip_reason"] == "manual_override_preserved"
    assert forced["change_count"] == 1
    assert forced["items"][0]["after"]["cadence_type"] == "weekly"
    assert forced["preview_id"] != preserved["preview_id"]
