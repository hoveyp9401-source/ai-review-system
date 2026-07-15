from datetime import datetime, timezone
import os

import pytest
from sqlalchemy import select

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    BusinessAuditEvent,
    CaseFollowupTask,
)
from app.agent2.business.policy import BusinessEffectPolicy
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.agent2.case_followup_commands import TriggerCaseFollowupNow
from app.db import AsyncSessionLocal


pytestmark = pytest.mark.skipif(
    os.getenv("AGENT2_REAL_POSTGRES_TEST") != "1",
    reason="real PostgreSQL integration is explicitly opt-in",
)


@pytest.mark.asyncio
async def test_real_postgresql_typed_manual_followup_is_atomic_and_rollback_safe():
    tenant_id = os.environ["AGENT2_REAL_POSTGRES_TENANT_ID"]
    user_id = os.environ["AGENT2_REAL_POSTGRES_USER_ID"]
    case_id = os.environ["AGENT2_REAL_POSTGRES_CASE_ID"]
    source_turn_id = "postgres-integration:case-followup:20260713"
    async with AsyncSessionLocal() as session:
        transaction = await session.begin()
        try:
            binding = await session.scalar(select(Agent2IdentityBinding).where(
                Agent2IdentityBinding.tenant_id == tenant_id,
                Agent2IdentityBinding.user_id == user_id,
                Agent2IdentityBinding.active.is_(True),
            ))
            case = await session.scalar(select(Agent2Case).where(
                Agent2Case.tenant_id == tenant_id,
                Agent2Case.case_id == case_id,
                Agent2Case.owner_user_id == user_id,
            ))
            assert binding is not None and case is not None
            assert case_id in {
                str(value) for value in (binding.permission_scope_json or {}).get(
                    "allowed_case_ids", []
                )
            }
            context = BusinessCommandContext(
                tenant_id=tenant_id, company_id=binding.company_id,
                department_id=binding.department_id, team_id=binding.team_id,
                actor_user_id=user_id, actor_role_ids=tuple(binding.role_ids or ()),
                allowed_case_ids=(case_id,), source_message_id=source_turn_id,
                source_channel="postgres_integration_test",
                occurred_at=datetime.now(timezone.utc),
                conversation_id=f"postgres-integration:{user_id}",
            )
            receipt = await SqlBusinessExecutor(
                session,
                effect_policy=BusinessEffectPolicy(
                    case_followup_policy_enabled=True,
                    case_followup_trigger_allowlist=frozenset({"manual"}),
                ),
            ).execute(
                TriggerCaseFollowupNow(
                    command_id=source_turn_id, tenant_id=tenant_id,
                    case_id=case_id, assigned_user_id=user_id,
                    source_turn_id=source_turn_id,
                    idempotency_key=source_turn_id,
                ),
                context,
            )
            assert receipt.status == "executed" and receipt.actual_write is True
            task = await session.scalar(select(CaseFollowupTask).where(
                CaseFollowupTask.tenant_id == tenant_id,
                CaseFollowupTask.followup_id == receipt.resource_id,
            ))
            audit = await session.scalar(select(BusinessAuditEvent).where(
                BusinessAuditEvent.tenant_id == tenant_id,
                BusinessAuditEvent.receipt_id == receipt.receipt_id,
            ))
            assert task is not None and task.task_status == "scheduled"
            assert audit is not None and audit.resource_id == receipt.resource_id
        finally:
            await transaction.rollback()

