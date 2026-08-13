import json
from uuid import UUID

import pytest

from app.agent2.tool_calling import production_store


@pytest.mark.asyncio
async def test_production_state_includes_authenticated_owner_weekly_payload(
    monkeypatch,
):
    captured = {}

    async def fake_weekly_payload(session, *, tenant_id, owner_user_id):
        captured.update(
            session=session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        return [{"plan_id": "plan-1", "version": 3}]

    monkeypatch.setattr(
        "app.agent2.weekly_plan_store.weekly_plan_state_payload",
        fake_weekly_payload,
    )

    class _Scalars:
        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def scalars(self, statement):
            del statement
            return _Scalars()

        async def execute(self, statement):
            del statement
            return _Result()

    session = _Session()
    user_id = UUID("10000000-0000-4000-8000-000000000001")

    state = await production_store.capture_production_state(
        session,
        tenant_id="tenant-a",
        user_id=user_id,
        conversation_id="direct-1",
        include_weekly_plan=True,
    )

    assert json.loads(state.canonical_json)["weekly_plans"] == [
        {"plan_id": "plan-1", "version": 3}
    ]
    assert captured == {
        "session": session,
        "tenant_id": "tenant-a",
        "owner_user_id": str(user_id),
    }
