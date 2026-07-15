from datetime import datetime, timedelta, timezone
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.agent2.business.contracts import (
    BusinessCommandContext,
    CreateTravelIntent,
    UpdateTravelIntent,
)
from app.agent2.business.models import (
    Agent2IdentityBinding,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    TravelIntent,
)
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.db import AsyncSessionLocal


pytestmark = pytest.mark.skipif(
    os.getenv("AGENT2_REAL_POSTGRES_TEST") != "1",
    reason="real PostgreSQL integration is explicitly opt-in",
)


@pytest.mark.asyncio
async def test_real_postgresql_distinct_messages_share_one_travel_fact_and_rollback():
    tenant_id = os.environ["AGENT2_REAL_POSTGRES_TENANT_ID"]
    user_id = os.environ["AGENT2_REAL_POSTGRES_USER_ID"]
    run_ref = uuid4().hex
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start_at = now + timedelta(days=30)
    end_at = start_at + timedelta(hours=8)
    source_ids = (f"travel-dedup-a:{run_ref}", f"travel-dedup-b:{run_ref}")
    resource_ids: tuple[str, ...] = ()
    receipt_ids: tuple[str, ...] = ()

    async with AsyncSessionLocal() as session:
        transaction = await session.begin()
        try:
            binding = await session.scalar(
                select(Agent2IdentityBinding).where(
                    Agent2IdentityBinding.tenant_id == tenant_id,
                    Agent2IdentityBinding.user_id == user_id,
                    Agent2IdentityBinding.active.is_(True),
                )
            )
            assert binding is not None

            receipts = []
            for index, source_id in enumerate(source_ids):
                command = CreateTravelIntent(
                    command_id=f"travel-dedup-command-{index}:{run_ref}",
                    destination_raw="PostgreSQL rollback test city",
                    destination_normalized="PostgreSQL rollback test city",
                    city_code="TEST-ROLLBACK",
                    province_code="TEST",
                    start_at=start_at,
                    end_at=end_at,
                    time_precision="exact",
                    purpose_summary=f"rollback-only travel dedup {run_ref}",
                    related_case_ids=(),
                    confidence=1.0,
                )
                context = BusinessCommandContext(
                    tenant_id=tenant_id,
                    company_id=binding.company_id,
                    department_id=binding.department_id,
                    team_id=binding.team_id,
                    actor_user_id=user_id,
                    actor_role_ids=tuple(binding.role_ids or ()),
                    allowed_case_ids=(),
                    source_message_id=source_id,
                    source_channel="postgres_integration_test",
                    occurred_at=now + timedelta(seconds=index),
                    conversation_id=f"postgres-integration:{user_id}",
                )
                receipts.append(
                    await SqlBusinessExecutor(
                        session,
                        execution_authority="legacy_user_compatibility",
                    ).execute(command, context)
                )

            first, duplicate = receipts
            assert (first.status, first.actual_write) == ("executed", True)
            assert (duplicate.status, duplicate.actual_write) == ("duplicate", False)
            assert duplicate.resource_id == first.resource_id
            assert duplicate.receipt_id != first.receipt_id
            resource_ids = (first.resource_id,)
            receipt_ids = (first.receipt_id, duplicate.receipt_id)

            intent_count = await session.scalar(
                select(func.count())
                .select_from(TravelIntent)
                .where(TravelIntent.travel_intent_id == UUID(first.resource_id))
            )
            receipt_count = await session.scalar(
                select(func.count())
                .select_from(BusinessCommandReceipt)
                .where(BusinessCommandReceipt.receipt_id.in_(receipt_ids))
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(BusinessAuditEvent)
                .where(BusinessAuditEvent.receipt_id.in_(receipt_ids))
            )
            assert intent_count == 1
            assert receipt_count == 2
            assert audit_count == 2

            cancel_context = BusinessCommandContext(
                **{
                    **context.as_dict(),
                    "source_message_id": f"travel-dedup-cancel:{run_ref}",
                    "occurred_at": now + timedelta(seconds=2),
                }
            )
            cancelled = await SqlBusinessExecutor(
                session,
                execution_authority="legacy_user_compatibility",
            ).execute(
                UpdateTravelIntent(
                    command_id=f"travel-dedup-cancel:{run_ref}",
                    travel_intent_id=first.resource_id,
                    expected_version=1,
                    status="cancelled",
                ),
                cancel_context,
            )
            recreate_context = BusinessCommandContext(
                **{
                    **context.as_dict(),
                    "source_message_id": f"travel-dedup-recreate:{run_ref}",
                    "occurred_at": now + timedelta(seconds=3),
                }
            )
            recreated = await SqlBusinessExecutor(
                session,
                execution_authority="legacy_user_compatibility",
            ).execute(
                CreateTravelIntent(
                    command_id=f"travel-dedup-recreate:{run_ref}",
                    destination_raw="PostgreSQL rollback test city",
                    destination_normalized="PostgreSQL rollback test city",
                    city_code="TEST-ROLLBACK",
                    province_code="TEST",
                    start_at=start_at,
                    end_at=end_at,
                    time_precision="exact",
                    purpose_summary=f"rollback-only travel dedup {run_ref}",
                    related_case_ids=(),
                    confidence=1.0,
                ),
                recreate_context,
            )
            assert (cancelled.status, cancelled.actual_write) == ("executed", True)
            assert (recreated.status, recreated.actual_write) == ("executed", True)
            assert recreated.resource_id != first.resource_id
            resource_ids = (first.resource_id, recreated.resource_id)
            receipt_ids = (
                first.receipt_id,
                duplicate.receipt_id,
                cancelled.receipt_id,
                recreated.receipt_id,
            )
            statuses = (
                await session.execute(
                    select(TravelIntent.status).where(
                        TravelIntent.travel_intent_id.in_(
                            tuple(UUID(value) for value in resource_ids)
                        )
                    )
                )
            ).scalars().all()
            assert sorted(statuses) == ["cancelled", "planned"]
        finally:
            await transaction.rollback()

    async with AsyncSessionLocal() as verification_session:
        assert await verification_session.scalar(
            select(func.count())
            .select_from(TravelIntent)
            .where(
                TravelIntent.travel_intent_id.in_(
                    tuple(UUID(value) for value in resource_ids)
                )
            )
        ) == 0
        assert await verification_session.scalar(
            select(func.count())
            .select_from(BusinessCommandReceipt)
            .where(BusinessCommandReceipt.receipt_id.in_(receipt_ids))
        ) == 0
