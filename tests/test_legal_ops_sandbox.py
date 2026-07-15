from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.legal_ops.api import LegalOpsRuntime, build_runtime, router
from app.legal_ops.auth import PrincipalDirectory, SandboxPrincipal
from app.legal_ops.repository import SandboxRepository
from app.legal_ops.seed import build_phase0_seed
from app.legal_ops.service import LegalOpsReadService


def _principals() -> PrincipalDirectory:
    return PrincipalDirectory.from_json(
        json.dumps(
            {
                "alpha-token": {
                    "tenant_id": "sandbox-alpha",
                    "user_id": "alpha-admin",
                    "role_ids": ["tenant_admin"],
                },
                "beta-token": {
                    "tenant_id": "sandbox-beta",
                    "user_id": "beta-reader",
                    "role_ids": ["tenant_reader"],
                },
            }
        )
    )


@pytest.fixture()
def repository(tmp_path: Path) -> SandboxRepository:
    repository = SandboxRepository(tmp_path / "legal-ops.json", sandbox_enabled=True)
    repository.reset(build_phase0_seed())
    return repository


def test_principal_is_resolved_server_side_and_rejects_unknown_token():
    directory = _principals()

    principal = directory.authenticate("alpha-token")

    assert principal.tenant_id == "sandbox-alpha"
    assert principal.user_id == "alpha-admin"
    assert "tenant_admin" in principal.role_ids
    with pytest.raises(PermissionError, match="invalid sandbox credential"):
        directory.authenticate("unknown")


def test_live_runtime_uses_two_distinct_server_side_principals_without_shared_admin_fallback(tmp_path: Path):
    settings = Settings(
        legal_ops_live_enabled=True,
        legal_ops_live_tenant_id="sandbox-agent2-phase2-20260711",
        legal_ops_live_principals_json=json.dumps(
            {
                "pang-token": {
                    "tenant_id": "sandbox-agent2-phase2-20260711",
                    "user_id": "pang-user",
                    "role_ids": ["case_owner"],
                },
                "liu-token": {
                    "tenant_id": "sandbox-agent2-phase2-20260711",
                    "user_id": "liu-user",
                    "role_ids": ["case_owner"],
                },
            }
        ),
        legal_ops_sandbox_data_path=str(tmp_path / "unused.json"),
    )

    runtime = build_runtime(settings)

    assert runtime.principals.authenticate("pang-token").user_id == "pang-user"
    assert runtime.principals.authenticate("liu-token").user_id == "liu-user"
    assert not runtime.principals.authenticate("pang-token").has_role("tenant_admin")


def test_repository_never_returns_cross_tenant_records(repository: SandboxRepository):
    alpha = repository.tenant_snapshot("sandbox-alpha")
    beta = repository.tenant_snapshot("sandbox-beta")

    assert alpha["tenant"]["id"] == "sandbox-alpha"
    assert beta["tenant"]["id"] == "sandbox-beta"
    assert alpha["cases"]
    assert beta["cases"]
    assert all(item["tenant_id"] == "sandbox-alpha" for item in alpha["cases"])
    assert all(item["tenant_id"] == "sandbox-beta" for item in beta["cases"])
    assert {item["id"] for item in alpha["cases"]}.isdisjoint({item["id"] for item in beta["cases"]})


def test_url_or_header_tenant_tampering_cannot_change_scope(repository: SandboxRepository):
    principal = _principals().authenticate("alpha-token")
    service = LegalOpsReadService(repository)

    payload = service.case_list(principal, requested_tenant_id="sandbox-beta")

    assert payload["scope"]["tenant_id"] == "sandbox-alpha"
    assert payload["scope"]["ignored_requested_tenant_id"] == "sandbox-beta"
    assert all(item["tenant_id"] == "sandbox-alpha" for item in payload["items"])


def test_company_and_department_scope_are_enforced_server_side(repository: SandboxRepository):
    service = LegalOpsReadService(repository)
    department_head = SandboxPrincipal(
        tenant_id="sandbox-alpha",
        user_id="alpha-owner-3",
        role_ids=("department_head",),
        company_ids=("alpha-company",),
        department_ids=("alpha-legal",),
    )
    wrong_company = SandboxPrincipal(
        tenant_id="sandbox-alpha",
        user_id="alpha-owner-3",
        role_ids=("department_head",),
        company_ids=("not-authorized",),
        department_ids=("alpha-legal",),
    )

    assert len(service.case_list(department_head)["items"]) == 12
    assert service.case_list(wrong_company)["items"] == []
    assert service.shell(wrong_company)["companies"] == []


def test_weekly_and_monthly_are_independent_from_daily(repository: SandboxRepository):
    principal = _principals().authenticate("alpha-token")
    service = LegalOpsReadService(repository)

    daily = service.period_dashboard(principal, "daily")
    weekly = service.period_dashboard(principal, "weekly")
    monthly = service.period_dashboard(principal, "monthly")

    assert daily["source_contract"]["adapter"] == "daily_form_adapter"
    assert weekly["source_contract"]["adapter"] == "weekly_form_adapter"
    assert monthly["source_contract"]["adapter"] == "monthly_form_adapter"
    assert weekly["source_contract"]["depends_on"] == []
    assert monthly["source_contract"]["depends_on"] == []
    assert {item["source_id"] for item in weekly["submissions"]}.isdisjoint(
        {item["source_id"] for item in daily["submissions"]}
    )


def test_provenance_and_ai_status_are_explicit(repository: SandboxRepository):
    principal = _principals().authenticate("alpha-token")
    detail = LegalOpsReadService(repository).case_detail(principal, "alpha-case-01")

    origins = {node["origin"]["status"] for node in detail["lifecycle"]["nodes"]}
    assert "system_fact" in origins
    assert "ai_summary" in origins
    ai_nodes = [node for node in detail["lifecycle"]["nodes"] if node["origin"]["status"].startswith("ai_")]
    assert ai_nodes
    assert all(node["origin"]["confirmed"] is False for node in ai_nodes)
    assert all(node["origin"]["generator"] for node in ai_nodes)
    assert all(
        {
            "content_origin",
            "generated_by",
            "confirmation_status",
            "reviewed_by",
            "reviewed_at",
            "source_type",
        }
        <= node["origin"].keys()
        for node in ai_nodes
    )


def test_source_inventory_is_machine_readable_and_complete(repository: SandboxRepository):
    sources = repository.tenant_snapshot("sandbox-alpha")["sources"]
    required = {
        "primary_key",
        "source_id",
        "source_name",
        "business_domain",
        "source_type",
        "table_or_endpoint",
        "tenant_field",
        "company_field",
        "department_field",
        "team_field",
        "person_field",
        "case_field",
        "period_field",
        "time_fields",
        "owner",
        "data_owner",
        "quality_rules",
        "data_quality",
        "sensitivity",
        "known_gaps",
        "update_method",
        "update_frequency",
        "sandbox_usable",
        "drilldown_supported",
        "recommended_adapter",
        "schema_version",
        "schema",
    }

    assert sources
    assert all(required <= source.keys() for source in sources)


def test_only_confirmed_metric_definitions_produce_formal_values(repository: SandboxRepository):
    principal = _principals().authenticate("alpha-token")
    metrics = LegalOpsReadService(repository).metric_center(principal)["metrics"]

    confirmed = next(item for item in metrics if item["definition_status"] == "confirmed")
    draft = next(item for item in metrics if item["definition_status"] == "draft")
    assert confirmed["formal"] is True
    assert isinstance(confirmed["value"], (int, float))
    assert confirmed["components"]
    assert draft["formal"] is False
    assert draft["value"] is None
    assert draft["warning"] == "not a formal metric"


def test_seed_reset_is_idempotent_and_forbidden_outside_sandbox(tmp_path: Path):
    seed = build_phase0_seed()
    repository = SandboxRepository(tmp_path / "legal-ops.json", sandbox_enabled=True)

    first = repository.reset(seed)
    second = repository.reset(seed)

    assert first["content_hash"] == second["content_hash"]
    assert first["record_counts"] == second["record_counts"]
    production_repository = SandboxRepository(tmp_path / "production.json", sandbox_enabled=False)
    with pytest.raises(PermissionError, match="sandbox-only"):
        production_repository.reset(seed)


def test_tenant_reset_preserves_other_tenant_snapshot(repository: SandboxRepository):
    before_beta = repository.tenant_snapshot("sandbox-beta")
    before_file = repository.path.read_text(encoding="utf-8")

    result = repository.reset_tenant(build_phase0_seed(), "sandbox-alpha")

    after_beta = repository.tenant_snapshot("sandbox-beta")
    assert result["scope"] == "tenant_only"
    assert result["tenant_id"] == "sandbox-alpha"
    assert after_beta == before_beta
    assert repository.path.read_text(encoding="utf-8") == before_file


def test_concurrent_repository_instances_serialize_tenant_resets(tmp_path: Path):
    path = tmp_path / "concurrent.json"
    seed = build_phase0_seed()
    first = SandboxRepository(path, sandbox_enabled=True)
    second = SandboxRepository(path, sandbox_enabled=True)
    first.reset(seed)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: args[0].reset_tenant(seed, args[1]),
                ((first, "sandbox-alpha"), (second, "sandbox-beta")),
            )
        )

    assert {result["tenant_id"] for result in results} == {"sandbox-alpha", "sandbox-beta"}
    assert first.verify_tenant("sandbox-alpha")["valid"] is True
    assert second.verify_tenant("sandbox-beta")["valid"] is True
    assert list(tmp_path.glob("*.tmp")) == []


def test_case_lifecycle_has_deep_tree_and_no_dangling_cross_tenant_refs(repository: SandboxRepository):
    principal = _principals().authenticate("alpha-token")
    detail = LegalOpsReadService(repository).case_detail(principal, "alpha-case-01")

    lifecycle = detail["lifecycle"]
    assert len(lifecycle["lanes"]) >= 5
    assert len(lifecycle["nodes"]) >= 10
    assert {node["kind"] for node in lifecycle["nodes"]} >= {
        "event",
        "party",
        "document",
        "work_record",
        "asset",
        "collection",
        "risk",
        "task",
        "external_clue",
    }
    assert repository.verify_tenant("sandbox-alpha")["valid"] is True


def test_every_case_current_phase_is_a_lane_and_timeline_has_program_events(repository: SandboxRepository):
    cases = repository.tenant_snapshot("sandbox-alpha")["cases"]

    for case in cases:
        lane_ids = {lane["id"] for lane in case["lifecycle"]["lanes"]}
        assert case["current_phase"] in lane_ids
        assert len(case["lifecycle"]["timeline_node_ids"]) >= 5


@pytest.fixture()
def api_client(repository: SandboxRepository) -> TestClient:
    app = FastAPI()
    app.state.legal_ops_runtime = LegalOpsRuntime(
        enabled=True,
        repository=repository,
        service=LegalOpsReadService(repository),
        principals=_principals(),
    )
    app.include_router(router)
    return TestClient(app)


def test_bff_requires_server_side_credential_and_ignores_tenant_query(api_client: TestClient):
    assert api_client.get("/legal-ops/api/overview").status_code == 401

    response = api_client.get(
        "/legal-ops/api/cases?tenant_id=sandbox-beta",
        headers={"X-Legal-Ops-Token": "alpha-token", "X-Tenant-Id": "sandbox-beta"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"]["tenant_id"] == "sandbox-alpha"
    assert payload["scope"]["ignored_requested_tenant_id"] == "sandbox-beta"
    assert all(item["tenant_id"] == "sandbox-alpha" for item in payload["items"])


def test_disabled_runtime_hides_app_shell(repository: SandboxRepository):
    app = FastAPI()
    app.state.legal_ops_runtime = LegalOpsRuntime(
        enabled=False,
        repository=repository,
        service=LegalOpsReadService(repository),
        principals=_principals(),
    )
    app.include_router(router)

    assert TestClient(app).get("/legal-ops/").status_code == 404


def test_live_runtime_requires_explicit_token_and_never_falls_back_to_seed(tmp_path: Path):
    settings = Settings(
        legal_ops_live_enabled=True,
        legal_ops_live_tenant_id="sandbox-agent2-phase2-20260711",
        legal_ops_live_token="live-test-token",
        legal_ops_sandbox_enabled=False,
        legal_ops_sandbox_data_path=str(tmp_path / "must-not-be-created.json"),
    )
    runtime = build_runtime(settings)
    app = FastAPI()
    app.state.legal_ops_runtime = runtime
    app.include_router(router)
    client = TestClient(app)
    headers = {"X-Legal-Ops-Token": "live-test-token"}

    shell = client.get("/legal-ops/api/shell", headers=headers)

    assert runtime.mode == "sandbox_live"
    assert shell.status_code == 200
    assert shell.json()["mode"] == "sandbox_live"
    assert shell.json()["tenant"]["id"] == "sandbox-agent2-phase2-20260711"
    assert client.get("/legal-ops/api/overview", headers=headers).status_code == 404
    assert not (tmp_path / "must-not-be-created.json").exists()


def test_live_and_demo_modes_cannot_be_enabled_together(tmp_path: Path):
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        build_runtime(
            Settings(
                legal_ops_live_enabled=True,
                legal_ops_live_tenant_id="tenant-test",
                legal_ops_live_token="live-test-token",
                legal_ops_sandbox_enabled=True,
                legal_ops_sandbox_data_path=str(tmp_path / "demo.json"),
            )
        )


def test_startup_fails_closed_on_seed_revision_mismatch(tmp_path: Path):
    path = tmp_path / "mismatched.json"
    old_seed = build_phase0_seed()
    old_seed["metadata"]["generator_revision"] = 0
    repository = SandboxRepository(path, sandbox_enabled=True)
    repository.reset(old_seed)
    before = path.read_text(encoding="utf-8")
    settings = Settings(
        legal_ops_sandbox_enabled=True,
        legal_ops_sandbox_data_path=str(path),
        legal_ops_sandbox_seed_manifest=str(
            Path("app/legal_ops/fixtures/phase0_manifest.json").resolve()
        ),
        legal_ops_sandbox_token="local-test-token",
    )

    with pytest.raises(RuntimeError, match="refusing startup overwrite"):
        build_runtime(settings)

    assert path.read_text(encoding="utf-8") == before


def test_cross_tenant_case_id_is_not_disclosed(api_client: TestClient):
    response = api_client.get(
        "/legal-ops/api/cases/beta-case-01",
        headers={"X-Legal-Ops-Token": "alpha-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "case not found in authorized tenant"


def test_seed_reset_requires_admin_and_explicit_confirmation(api_client: TestClient):
    reader = {"X-Legal-Ops-Token": "beta-token", "X-Legal-Ops-Reset-Confirm": "legal-ops-product-redesign-v8"}
    admin = {"X-Legal-Ops-Token": "alpha-token", "X-Legal-Ops-Reset-Confirm": "legal-ops-product-redesign-v8"}

    assert api_client.post("/legal-ops/api/admin/reset", headers=reader).status_code == 403
    assert api_client.post(
        "/legal-ops/api/admin/reset", headers={"X-Legal-Ops-Token": "alpha-token"}
    ).status_code == 409
    response = api_client.post("/legal-ops/api/admin/reset", headers=admin)

    assert response.status_code == 200
    assert response.json()["reset"]["fixture"] is True
    assert response.json()["verification"]["valid"] is True
