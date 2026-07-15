from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.legal_ops.api import LegalOpsRuntime, router
from app.legal_ops.auth import PrincipalDirectory
from app.legal_ops.repository import SandboxRepository
from app.legal_ops.seed import build_phase0_seed
from app.legal_ops.service import LegalOpsReadService


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    repository = SandboxRepository(tmp_path / "legal-ops-e2e.json", sandbox_enabled=True)
    repository.reset(build_phase0_seed())
    principals = PrincipalDirectory.from_json(
        json.dumps(
            {
                "admin-a": {"tenant_id": "sandbox-alpha", "user_id": "alpha-admin", "role_ids": ["tenant_admin"]},
                "reader-b": {"tenant_id": "sandbox-beta", "user_id": "beta-reader", "role_ids": ["tenant_reader"]},
                "member-a": {
                    "tenant_id": "sandbox-alpha",
                    "user_id": "alpha-owner-1",
                    "role_ids": ["case_owner"],
                    "team_ids": ["alpha-dispute"],
                },
            }
        )
    )
    app = FastAPI()
    app.state.legal_ops_runtime = LegalOpsRuntime(True, repository, LegalOpsReadService(repository), principals)
    app.include_router(router)
    return TestClient(app)


def _get(client: TestClient, path: str, token: str = "admin-a"):
    return client.get(path, headers={"X-Legal-Ops-Token": token})


def test_phase0_e2e_01_leader_opens_shell_and_gets_company_team_filters(client: TestClient):
    payload = _get(client, "/legal-ops/api/shell").json()
    assert payload["companies"] and len(payload["teams"]) == 3
    assert payload["navigation"] == [
        "command",
        "work",
        "performance",
        "teams",
        "forest",
        "travel",
    ]


def test_phase0_e2e_02_daily_team_detail(client: TestClient):
    payload = _get(client, "/legal-ops/api/reports/daily").json()
    assert payload["summary"]["total"] == 24
    assert {item["team_id"] for item in payload["submissions"]} >= {"alpha-dispute", "alpha-contract"}


def test_daily_history_contains_real_work_revisions_deletions_and_cross_module_links(client: TestClient):
    payload = _get(client, "/legal-ops/api/reports/daily").json()
    submissions = payload["submissions"]

    assert len({item["period"] for item in submissions}) == 6
    assert all(item["work_items"] and item["tomorrow_plan"] for item in submissions)
    assert all(item["user_name"] and item["source_display"] == "来源：日报机器人" for item in submissions)
    assert any(item["revisions"] for item in submissions)
    assert any(item["deleted_items"] for item in submissions)
    assert any(item["linked_travel_ids"] for item in submissions)
    assert all(item["linked_case_ids"] for item in submissions)
    assert not any("原始 Sandbox 表单文本" in item["raw_text"] for item in submissions)


def test_phase0_e2e_03_daily_person_raw_text(client: TestClient):
    payload = _get(client, "/legal-ops/api/reports/daily/alpha-daily-02").json()
    assert payload["raw_text"]
    assert payload["user_id"] == "alpha-owner-1"


def test_phase0_e2e_04_daily_drills_to_related_case(client: TestClient):
    payload = _get(client, "/legal-ops/api/reports/daily/alpha-daily-02").json()
    case_id = payload["linked_case_ids"][0]
    assert _get(client, f"/legal-ops/api/cases/{case_id}").status_code == 200


def test_phase0_e2e_05_case_lifecycle_opens(client: TestClient):
    payload = _get(client, "/legal-ops/api/cases/alpha-case-01").json()
    assert payload["lifecycle"]["root_id"]
    assert len(payload["lifecycle"]["nodes"]) >= 10


def test_phase0_e2e_06_legal_procedure_backbone(client: TestClient):
    payload = _get(client, "/legal-ops/api/cases/alpha-case-01").json()
    assert [lane["id"] for lane in payload["lifecycle"]["lanes"]] == ["intake", "filing", "trial", "execution", "closure"]


def test_phase0_e2e_07_event_source_and_evidence(client: TestClient):
    nodes = _get(client, "/legal-ops/api/cases/alpha-case-01").json()["lifecycle"]["nodes"]
    assert any(node["kind"] == "document" and node["origin"]["source_id"] for node in nodes)


def test_phase0_e2e_08_execution_collection_risk_and_task(client: TestClient):
    kinds = {node["kind"] for node in _get(client, "/legal-ops/api/cases/alpha-case-01").json()["lifecycle"]["nodes"]}
    assert {"asset", "collection", "risk", "task"} <= kinds


def test_phase0_e2e_09_lifecycle_returns_to_source_daily(client: TestClient):
    payload = _get(client, "/legal-ops/api/cases/alpha-case-02").json()
    daily_id = payload["drilldowns"]["daily_submission_ids"][0]
    assert _get(client, f"/legal-ops/api/reports/daily/{daily_id}").status_code == 200


def test_case_forest_has_real_party_hierarchy_and_differentiated_lifecycles(client: TestClient):
    forest = _get(client, "/legal-ops/api/forest").json()
    cases = _get(client, "/legal-ops/api/cases").json()["items"]

    assert len(forest["roots"]) >= 3
    assert all(root["defendants"] for root in forest["roots"])
    assert all(case["plaintiff"] and case["defendants"] and case["case_number"] for case in cases)
    assert len({len(case["lifecycle"]["nodes"]) for case in cases}) >= 4
    assert any(case["stagnation_days"] >= 20 for case in cases)


def test_phase0_e2e_10_weekly_dashboard(client: TestClient):
    payload = _get(client, "/legal-ops/api/reports/weekly").json()
    assert payload["source_contract"]["adapter"] == "weekly_form_adapter"
    assert payload["source_contract"]["depends_on"] == []


def test_phase0_e2e_11_weekly_metric_to_raw_submission(client: TestClient):
    payload = _get(client, "/legal-ops/api/reports/weekly/alpha-weekly-01").json()
    assert payload["raw_text"] and payload["adapter"] == "weekly_form_adapter"


def test_phase0_e2e_12_monthly_dashboard(client: TestClient):
    payload = _get(client, "/legal-ops/api/reports/monthly").json()
    assert payload["source_contract"]["adapter"] == "monthly_form_adapter"
    assert payload["source_contract"]["depends_on"] == []


def test_phase0_e2e_13_monthly_metric_to_raw_submission(client: TestClient):
    payload = _get(client, "/legal-ops/api/reports/monthly/alpha-monthly-01").json()
    assert payload["raw_text"] and payload["adapter"] == "monthly_form_adapter"


def test_phase0_e2e_14_performance_metric_drilldown(client: TestClient):
    metrics = _get(client, "/legal-ops/api/metrics").json()["metrics"]
    formal = next(item for item in metrics if item["code"] == "open_case_count")
    assert formal["formal"] is True and formal["components"]
    assert all("team_id" in item and "user_id" in item for item in formal["components"])


def test_product_redesign_performance_center_has_all_sources_collection_and_export_contracts(client: TestClient):
    payload = _get(client, "/legal-ops/api/performance").json()

    assert {item["period_type"] for item in payload["report_runs"]} == {"weekly", "monthly"}
    assert {item["data_source_type"] for item in payload["metrics"]} >= {
        "workbuddy_skill",
        "manual",
        "robot_collection",
    }
    assert {item["confirmation_status"] for item in payload["target_collections"]} >= {
        "已确认",
        "已回复",
        "未回复",
    }
    assert payload["manual_entries"]
    assert {item["format"] for item in payload["export_formats"]} == {"docx", "pdf", "xlsx"}
    information_center = next(item for item in payload["source_types"] if item["code"] == "information_center")
    assert information_center["connected"] is False


@pytest.mark.parametrize(
    ("period", "file_format", "magic"),
    [
        ("weekly", "docx", b"PK"),
        ("weekly", "pdf", b"%PDF"),
        ("weekly", "xlsx", b"PK"),
        ("monthly", "docx", b"PK"),
        ("monthly", "pdf", b"%PDF"),
        ("monthly", "xlsx", b"PK"),
    ],
)
def test_performance_exports_are_downloadable_and_nonempty(
    client: TestClient, period: str, file_format: str, magic: bytes
):
    response = _get(client, f"/legal-ops/api/exports/{period}/{file_format}")

    assert response.status_code == 200
    assert response.content.startswith(magic)
    assert len(response.content) > 1_000
    assert f"legal-ops-{period}-report.{file_format}" in response.headers["content-disposition"]


def test_performance_export_rejects_unknown_period_or_format(client: TestClient):
    assert _get(client, "/legal-ops/api/exports/yearly/pdf").status_code == 422
    assert _get(client, "/legal-ops/api/exports/weekly/exe").status_code == 422


def test_phase0_e2e_15_travel_to_related_case(client: TestClient):
    travel = _get(client, "/legal-ops/api/travel").json()["travels"][0]
    assert _get(client, f"/legal-ops/api/cases/{travel['case_id']}").status_code == 200


def test_team_and_travel_workbenches_expose_cross_module_operating_signals(client: TestClient):
    teams = _get(client, "/legal-ops/api/teams").json()["teams"]
    travels = _get(client, "/legal-ops/api/travel").json()["travels"]

    assert all(team["members"] and 0 <= team["daily_completion_rate"] <= 100 for team in teams)
    assert all("stagnant_case_count" in team and "active_travel_count" in team for team in teams)
    assert all(travel["traveler_name"] and travel["case_title"] for travel in travels)
    assert any(travel["collaboration_candidates"] for travel in travels)


def test_phase0_e2e_16_tenant_a_cannot_access_tenant_b(client: TestClient):
    assert _get(client, "/legal-ops/api/cases/beta-case-01", "admin-a").status_code == 404


def test_phase0_e2e_17_member_cannot_access_unauthorized_team(client: TestClient):
    assert _get(client, "/legal-ops/api/cases?team_id=alpha-contract", "member-a").status_code == 404
    payload = _get(client, "/legal-ops/api/cases", "member-a").json()
    assert payload["items"] and all(item["team_id"] == "alpha-dispute" for item in payload["items"])
    quality = _get(client, "/legal-ops/api/quality", "member-a").json()
    assert quality["isolation_verification"]["scope"] == "authorized_subset"
    assert quality["isolation_verification"]["record_counts"]["cases"] == len(payload["items"])


def test_phase0_e2e_18_seed_reset_restores_system(client: TestClient):
    response = client.post(
        "/legal-ops/api/admin/reset",
        headers={"X-Legal-Ops-Token": "admin-a", "X-Legal-Ops-Reset-Confirm": "legal-ops-product-redesign-v8"},
    )
    assert response.status_code == 200
    assert response.json()["verification"]["valid"] is True
