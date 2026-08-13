from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent2.weekly_plan_domain import create_weekly_plan, create_weekly_plan_batch
from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.agent2.weekly_plan_stats import WeeklyPlanMondayStatsReader
from app.agent2.weekly_plan_store import InMemoryWeeklyPlanStore
from app.config import Settings, get_settings
from app.legal_ops.api import (
    LegalOpsRuntime,
    get_weekly_plan_monday_stats_reader,
    get_weekly_plan_stats_now,
    router,
)
from app.legal_ops.auth import PrincipalDirectory, SandboxPrincipal


DEADLINE = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
READ_AT = DEADLINE + timedelta(hours=2)


class _AsyncMemoryStatsStore:
    def __init__(self, memory: InMemoryWeeklyPlanStore) -> None:
        self._memory = memory

    async def load_monday_snapshot(self, **kwargs):
        return self._memory.load_monday_snapshot(**kwargs)

    async def reconcile_monday_snapshot(self, **kwargs):
        return self._memory.reconcile_monday_snapshot(**kwargs)


def _client(
    *,
    include_other_in_snapshot: bool = False,
    settings_overrides: dict[str, object] | None = None,
) -> tuple[TestClient, str]:
    member = WeeklyPlanRosterMember(
        user_id="user-a",
        display_name="测试用户甲",
        department_id="management",
        department_name="综合管理部",
    )
    roster = (member,)
    if include_other_in_snapshot:
        roster += (
            WeeklyPlanRosterMember(user_id="other-user", display_name="其他用户"),
        )
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=roster,
        created_at=DEADLINE - timedelta(days=3),
    )
    memory = InMemoryWeeklyPlanStore()
    memory.save_batch(batch)
    memory.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE,
        deadline_at=DEADLINE,
    )
    memory.save_plan(
        replace(
            create_weekly_plan(
                batch=batch,
                owner_user_id="user-a",
                created_at=DEADLINE + timedelta(minutes=5),
            ),
            status="submitted",
            version=1,
            submitted_at=DEADLINE + timedelta(minutes=5),
            updated_at=DEADLINE + timedelta(minutes=5),
        )
    )
    principals = PrincipalDirectory(
        {
            "user-a-token": SandboxPrincipal(
                tenant_id="tenant-a",
                user_id="user-a",
                role_ids=("case_owner",),
            ),
            "other-token": SandboxPrincipal(
                tenant_id="tenant-a",
                user_id="other-user",
                role_ids=("tenant_admin",),
            ),
        }
    )
    app = FastAPI()
    app.state.legal_ops_runtime = LegalOpsRuntime(
        enabled=True,
        repository=None,  # type: ignore[arg-type]
        service=None,  # type: ignore[arg-type]
        principals=principals,
        mode="sandbox_live",
    )
    app.include_router(router)
    settings_values: dict[str, object] = {
        "legal_ops_live_enabled": True,
        "legal_ops_live_tenant_id": "tenant-a",
        "legal_ops_live_token": "unused",
        "agent2_weekly_plan_enabled": True,
        "agent2_weekly_plan_tenant_allowlist": "tenant-a",
        "agent2_weekly_plan_user_allowlist": "user-a",
    }
    settings_values.update(settings_overrides or {})
    app.dependency_overrides[get_settings] = lambda: Settings(**settings_values)
    app.dependency_overrides[get_weekly_plan_monday_stats_reader] = lambda: (
        WeeklyPlanMondayStatsReader(_AsyncMemoryStatsStore(memory))
    )
    app.dependency_overrides[get_weekly_plan_stats_now] = lambda: READ_AT
    return TestClient(app), batch.batch_id


def test_canary_reads_frozen_monday_baseline_and_later_delta_from_live_api() -> None:
    client, batch_id = _client()

    response = client.get(
        f"/legal-ops/api/workspace/weekly-plans/{batch_id}/monday-stats",
        headers={"X-Legal-Ops-Token": "user-a-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_id"] == "tenant-a"
    assert payload["batch_id"] == batch_id
    assert payload["frozen_snapshot"] == {
        "snapshot_id": payload["frozen_snapshot"]["snapshot_id"],
        "as_of": DEADLINE.isoformat().replace("+00:00", "Z"),
        "deadline_at": DEADLINE.isoformat().replace("+00:00", "Z"),
        "roster_count": 1,
        "submitted_count": 0,
        "draft_count": 0,
        "unfilled_count": 1,
        "rows": [
            {
                "user_id": "user-a",
                "display_name": "测试用户甲",
                "plan_status": "unfilled",
                "plan_version": 0,
                "submitted_at": None,
            }
        ],
    }
    assert payload["after_snapshot"] == {
        "reconciled_at": READ_AT.isoformat().replace("+00:00", "Z"),
        "current_submitted_count": 1,
        "late_submitted_count": 1,
        "closed_window_submitted_count": 0,
        "changed_after_snapshot_count": 1,
        "rows": [
            {
                "user_id": "user-a",
                "snapshot_plan_status": "unfilled",
                "current_plan_status": "submitted",
                "submission_timing": "late",
                "changed_after_snapshot": True,
            }
        ],
    }


def test_weekly_plan_stats_stay_hidden_from_everyone_outside_the_one_canary() -> None:
    client, batch_id = _client()

    response = client.get(
        f"/legal-ops/api/workspace/weekly-plans/{batch_id}/monday-stats",
        headers={"X-Legal-Ops-Token": "other-token"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


def test_canary_endpoint_refuses_a_batch_whose_frozen_roster_contains_anyone_else() -> None:
    client, batch_id = _client(include_other_in_snapshot=True)

    response = client.get(
        f"/legal-ops/api/workspace/weekly-plans/{batch_id}/monday-stats",
        headers={"X-Legal-Ops-Token": "user-a-token"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


def test_weekly_plan_stats_fail_closed_for_multiple_or_display_name_scopes() -> None:
    for user_scope in ("user-a,other-user", "中文姓名"):
        client, batch_id = _client(
            settings_overrides={"agent2_weekly_plan_user_allowlist": user_scope}
        )

        response = client.get(
            f"/legal-ops/api/workspace/weekly-plans/{batch_id}/monday-stats",
            headers={"X-Legal-Ops-Token": "user-a-token"},
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Not found"}


def test_unknown_batch_is_not_disclosed_by_the_canary_endpoint() -> None:
    client, _ = _client()

    response = client.get(
        "/legal-ops/api/workspace/weekly-plans/00000000-0000-0000-0000-000000000001/monday-stats",
        headers={"X-Legal-Ops-Token": "user-a-token"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
