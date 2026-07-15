from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.legal_ops.auth import SandboxPrincipal
from app.legal_ops.repository import SandboxRepository
from app.legal_ops.seed import build_phase0_seed
from app.legal_ops.service import LegalOpsReadService


STATIC = ROOT / "app" / "legal_ops" / "static"
OUTPUT_ROOT = ROOT / "artifacts" / "legal-ops-local-gpt"
ZIP_PATH = ROOT / "artifacts" / "legal-ops-local-gpt.zip"


def main() -> None:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_ROOT.mkdir(parents=True)

    responses = _offline_responses()
    html = _standalone_html(responses)
    (OUTPUT_ROOT / "index.html").write_text(html, encoding="utf-8")
    (OUTPUT_ROOT / "offline-data.json").write_text(
        json.dumps(responses, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUTPUT_ROOT / "README.md").write_text(_readme(), encoding="utf-8")
    (OUTPUT_ROOT / "manifest.json").write_text(
        json.dumps(
            {
                "artifact": "legal-ops-local-gpt",
                "kind": "self-contained-offline-web-demo",
                "entrypoint": "index.html",
                "data": "synthetic sandbox fixtures only",
                "network_required": False,
                "generated_from": "app/legal_ops",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    _copy_sources()
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(OUTPUT_ROOT.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(OUTPUT_ROOT.parent))

    print(
        json.dumps(
            {
                "directory": str(OUTPUT_ROOT),
                "zip": str(ZIP_PATH),
                "html_bytes": (OUTPUT_ROOT / "index.html").stat().st_size,
                "zip_bytes": ZIP_PATH.stat().st_size,
            },
            ensure_ascii=False,
        )
    )


def _offline_responses() -> dict[str, Any]:
    seed = build_phase0_seed()
    with tempfile.TemporaryDirectory(prefix="legal-ops-export-") as temp_dir:
        repository = SandboxRepository(Path(temp_dir) / "snapshot.json", sandbox_enabled=True)
        repository.reset(seed)
        service = LegalOpsReadService(repository)
        principal = SandboxPrincipal(
            tenant_id="sandbox-alpha",
            user_id="alpha-admin",
            role_ids=("tenant_admin", "sandbox_admin"),
        )
        responses: dict[str, Any] = {
            "shell": service.shell(principal),
            "overview": service.overview(principal),
            "reports/daily": service.period_dashboard(principal, "daily"),
            "reports/weekly": service.period_dashboard(principal, "weekly"),
            "reports/monthly": service.period_dashboard(principal, "monthly"),
            "teams": service.team_workbench(principal),
            "travel": service.travel_dashboard(principal),
            "metrics": service.metric_center(principal),
            "performance": service.performance_center(principal),
            "cases": service.case_list(principal),
            "quality": service.quality_center(principal),
            "sources": service.source_center(principal),
            "permissions": service.permission_center(principal),
        }
        for period in ("daily", "weekly", "monthly"):
            dashboard = responses[f"reports/{period}"]
            for item in dashboard["submissions"]:
                responses[f"reports/{period}/{item['id']}"] = service.report_detail(
                    principal,
                    period,
                    item["id"],
                )
        for item in responses["cases"]["items"]:
            responses[f"cases/{item['id']}"] = service.case_detail(principal, item["id"])

    phase2, details = _phase2_demo(seed)
    responses["phase2"] = phase2
    responses.update({f"phase2/cases/{key}": value for key, value in details.items()})
    return responses


def _phase2_demo(seed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cases = [item for item in seed["cases"] if item["tenant_id"] == "sandbox-alpha"][:4]
    phase2_cases = [
        {
            "case_id": item["id"],
            "external_case_id": item["id"],
            "case_number": f"（2026）演示{index:03d}号",
            "case_name": item["title"],
            "status": item["status"],
            "owner_user_id": item["owner_user_id"],
            "team_id": item["team_id"],
        }
        for index, item in enumerate(cases, start=1)
    ]
    parties = [
        {"party_id": "party-huadong", "canonical_name": "南京华东建设有限公司", "party_type": "company", "unified_social_credit_code": "91320100DEMO001", "data_quality": "confirmed_identifier"},
        {"party_id": "party-wangxi", "canonical_name": "王喜", "party_type": "person", "unified_social_credit_code": "", "data_quality": "confirmed_source"},
        {"party_id": "party-south", "canonical_name": "南京南方供应链有限公司", "party_type": "company", "unified_social_credit_code": "91320100DEMO002", "data_quality": "confirmed_identifier"},
        {"party_id": "party-bank", "canonical_name": "江苏演示商业银行", "party_type": "organization", "unified_social_credit_code": "", "data_quality": "imported_record"},
    ]
    roles = [
        {"role_id": f"role-{index}", "party_id": parties[index % len(parties)]["party_id"], "case_id": item["case_id"], "role_type": "defendant" if index % 2 else "plaintiff"}
        for index, item in enumerate(phase2_cases)
    ]
    clues = [
        {"clue_id": "clue-payment", "case_id": phase2_cases[0]["case_id"], "party_id": "party-huadong", "clue_type": "payment", "summary": "回款 12 万元，等待财务复核", "amount": 120000, "source_type": "payment_ledger", "source_id": "payment-demo-1"},
        {"clue_id": "clue-court", "case_id": phase2_cases[0]["case_id"], "party_id": "party-huadong", "clue_type": "court", "summary": "南京中院执行窗口", "amount": None, "source_type": "case_registry", "source_id": "court-demo-1"},
        {"clue_id": "clue-asset", "case_id": phase2_cases[1]["case_id"], "party_id": "party-wangxi", "clue_type": "asset", "summary": "发现待核验不动产线索", "amount": None, "source_type": "external_clue", "source_id": "asset-demo-1"},
    ]
    travels = [
        {"travel_intent_id": "travel-zhang", "user_id": "alpha-owner-1", "destination_normalized": "南京市", "start_at": "2026-07-15T09:00:00+08:00", "end_at": "2026-07-16T18:00:00+08:00", "status": "confirmed"},
        {"travel_intent_id": "travel-li", "user_id": "alpha-owner-2", "destination_normalized": "南京市", "start_at": "2026-07-15T13:00:00+08:00", "end_at": "2026-07-15T20:00:00+08:00", "status": "confirmed"},
    ]
    notifications = [
        {"notification_id": "notify-zhang", "recipient_user_id": "alpha-owner-1", "status": "sent", "retry_count": 0, "external_message_id": "demo-msg-001", "error_message": ""},
        {"notification_id": "notify-li", "recipient_user_id": "alpha-owner-2", "status": "sent", "retry_count": 0, "external_message_id": "demo-msg-002", "error_message": ""},
    ]
    progress = [
        {"progress_id": "progress-1", "case_id": phase2_cases[0]["case_id"], "summary": "已与南京中院沟通，预计本周五反馈", "content_origin": "human_record", "version": 2, "reporter_id": "alpha-owner-1", "source_message_id": "demo-source-001"},
        {"progress_id": "progress-2", "case_id": phase2_cases[1]["case_id"], "summary": "今日开庭，对方提出调解方案", "content_origin": "human_record", "version": 1, "reporter_id": "alpha-owner-2", "source_message_id": "demo-source-002"},
    ]
    receipts = [
        {"receipt_id": "receipt-daily-001", "command_type": "append_item", "status": "executed", "source_message_id": "demo-source-001", "resource_type": "daily_report", "actual_write": True, "failed_stage": ""},
        {"receipt_id": "receipt-progress-001", "command_type": "create_case_progress", "status": "executed", "source_message_id": "demo-source-001", "resource_type": "case_progress", "actual_write": True, "failed_stage": ""},
        {"receipt_id": "receipt-travel-001", "command_type": "create_travel_intent", "status": "duplicate", "source_message_id": "demo-source-003", "resource_type": "travel_intent", "actual_write": False, "failed_stage": "idempotent_replay"},
    ]
    audits = [
        {"audit_id": "audit-001", "receipt_id": "receipt-daily-001", "command_type": "append_item", "actor_user_id": "alpha-owner-1", "source_message_id": "demo-source-001", "resource_type": "daily_report"},
        {"audit_id": "audit-002", "receipt_id": "receipt-progress-001", "command_type": "create_case_progress", "actor_user_id": "alpha-owner-1", "source_message_id": "demo-source-001", "resource_type": "case_progress"},
    ]
    summary = {
        "parties": len(parties),
        "party_case_roles": len(roles),
        "party_relations": 1,
        "party_case_clues": len(clues),
        "cases": len(phase2_cases),
        "travel_intents": len(travels),
        "collaboration_candidates": 1,
        "notifications": len(notifications),
        "case_progress": len(progress),
        "receipts": len(receipts),
        "audits": len(audits),
        "failures": 0,
    }
    phase2 = {
        "tenant_id": "sandbox-alpha",
        "route_control": {"route_mode": "agent2_shadow", "agent1_rollback_enabled": False, "version": 3},
        "summary": summary,
        "parties": parties,
        "party_case_roles": roles,
        "party_relations": [{"relation_id": "relation-1", "case_id": phase2_cases[0]["case_id"], "relation_type": "legal_representative"}],
        "party_case_clues": clues,
        "cases": phase2_cases,
        "travel_intents": travels,
        "collaboration_candidates": [{"candidate_id": "candidate-1", "destination": "南京市", "status": "accepted", "participant_ids": ["alpha-owner-1", "alpha-owner-2"]}],
        "notifications": notifications,
        "case_progress": progress,
        "receipts": receipts,
        "audits": audits,
    }
    details: dict[str, Any] = {}
    for index, source in enumerate(cases):
        case = phase2_cases[index]
        nodes = [node for node in source["lifecycle"]["nodes"] if node["kind"] in {"event", "work_record", "risk"}][:6]
        details[case["case_id"]] = {
            "tenant_id": "sandbox-alpha",
            "case": case,
            "parties": [
                {"party": parties[index % len(parties)], "role": roles[index % len(roles)]}
            ],
            "business_clues": [item for item in clues if item["case_id"] == case["case_id"]],
            "lifecycle": {
                "internal_progress_nodes": [
                    {
                        "node_id": node["id"],
                        "kind": "internal_progress",
                        "progress_id": f"offline-{node['id']}",
                        "occurred_at": node["occurred_at"],
                        "recorded_at": node["occurred_at"],
                        "title": node["title"],
                        "details": "Sandbox 离线演示节点",
                        "progress_type": node["kind"],
                        "content_origin": node["origin"]["content_origin"],
                        "confirmation_status": node["origin"]["confirmation_status"],
                        "source_message_id": node["origin"]["source_id"],
                        "source_channel": "offline_fixture",
                        "reporter_id": case["owner_user_id"],
                        "version": 1,
                        "deleted": False,
                        "deleted_at": None,
                    }
                    for node in nodes
                ]
            },
        }
    return phase2, details


def _standalone_html(responses: dict[str, Any]) -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    js = js.replace(
        'token: sessionStorage.getItem("legalOpsSandboxToken") || "",',
        'token: "offline-demo",',
    )
    api_start = js.index("async function api(path) {")
    api_end = js.index("\n}\n\nfunction badge", api_start) + 2
    payload = json.dumps(responses, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    offline_api = f'''const OFFLINE_RESPONSES = {payload};

async function api(path) {{
  const [route, queryString = ""] = String(path || "").split("?");
  if (route === "cases") {{
    const params = new URLSearchParams(queryString);
    const base = structuredClone(OFFLINE_RESPONSES.cases);
    let items = base.items;
    if (params.get("status")) items = items.filter(item => item.status === params.get("status"));
    if (params.get("risk")) items = items.filter(item => item.risk_level === params.get("risk"));
    if (params.get("team_id")) items = items.filter(item => item.team_id === params.get("team_id"));
    if (params.get("query")) {{
      const needle = params.get("query").toLowerCase();
      items = items.filter(item => item.title.toLowerCase().includes(needle) || item.id.toLowerCase().includes(needle));
    }}
    return {{ ...base, items, total: items.length }};
  }}
  const value = OFFLINE_RESPONSES[route];
  if (value === undefined) throw new Error(`离线包没有该只读接口：${{route}}`);
  return structuredClone(value);
}}

async function downloadExport(period, fileFormat) {{
  const anchor = document.createElement("a");
  anchor.href = `exports/legal-ops-${{period}}-report.${{fileFormat}}`;
  anchor.download = `legal-ops-${{period}}-report.${{fileFormat}}`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  toast(`${{period === "weekly" ? "周报" : "月报"}} ${{fileFormat.toUpperCase()}} 已生成`);
}}'''
    js = js[:api_start] + offline_api + js[api_end:]
    html = html.replace(
        '<link rel="stylesheet" href="/legal-ops/assets/styles.css?v=10" />',
        f"<style>\n{css}\n</style>",
    )
    html = html.replace(
        '<script src="/legal-ops/assets/app.js?v=10" defer></script>',
        f"<script>\n{js}\n</script>",
    )
    html = html.replace("Phase 0 · Sandbox", "本地离线演示")
    html = html.replace("法务运营中台 · Sandbox", "法务运营中台 · GPT 离线交付版")
    return html


def _copy_sources() -> None:
    frontend = OUTPUT_ROOT / "source" / "frontend"
    backend = OUTPUT_ROOT / "source" / "backend"
    docs = OUTPUT_ROOT / "docs"
    frontend.mkdir(parents=True)
    backend.mkdir(parents=True)
    docs.mkdir(parents=True)
    export_dir = OUTPUT_ROOT / "exports"
    export_dir.mkdir(parents=True)
    for name in ("index.html", "styles.css", "app.js"):
        shutil.copy2(STATIC / name, frontend / name)
    for source in (
        ROOT / "app" / "legal_ops" / "api.py",
        ROOT / "app" / "legal_ops" / "service.py",
        ROOT / "app" / "legal_ops" / "phase2_read.py",
        ROOT / "app" / "legal_ops" / "seed.py",
        ROOT / "app" / "legal_ops" / "fixtures" / "phase0_manifest.json",
    ):
        shutil.copy2(source, backend / source.name)
    for source in (
        ROOT / "docs" / "AGENT2_PHASE2_ACCEPTANCE_EVIDENCE_20260711.md",
        ROOT / "docs" / "AGENT2_PHASE2_MIGRATION.md",
        ROOT / "docs" / "LEGAL_OPERATIONS_PHASE0_RUNBOOK.md",
    ):
        shutil.copy2(source, docs / source.name)
    for source in sorted((ROOT / "artifacts" / "legal-ops-exports").glob("legal-ops-*-report.*")):
        if source.suffix in {".docx", ".pdf", ".xlsx"}:
            shutil.copy2(source, export_dir / source.name)


def _readme() -> str:
    return """# 法务运营中台 · GPT 离线交付版

## 怎么看

直接双击 `index.html`。页面不需要服务器、数据库、账号、网络或构建工具。

## 包含内容

- `index.html`：单文件离线演示，内嵌样式、交互和 Sandbox 数据。
- `offline-data.json`：页面使用的完整离线只读数据。
- `source/frontend/`：原始前端源码。
- `source/backend/`：核心 BFF、读模型、Seed 与 Phase 2 读取实现。
- `docs/`：架构和验收说明。

## 给 GPT 的建议提示词

> 这是一个法务运营中台的本地离线包。请先阅读 README、manifest、前端源码、后端读模型和验收文档，再从信息架构、产品流程、权限边界、Agent2 验收证据与视觉设计五个维度评审。不要把离线 Sandbox 数据当成生产事实。

## 数据边界

全部业务数据都是合成的 Sandbox fixtures。包内不包含密码、Token、Cookie、服务器地址或真实客户信息。
"""


if __name__ == "__main__":
    main()
