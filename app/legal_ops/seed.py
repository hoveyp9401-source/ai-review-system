from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any


FIXTURE_PATH = Path(__file__).with_name("fixtures") / "phase0_manifest.json"
SEED_GENERATOR_REVISION = 9
FICTIONAL_CASE_PLAINTIFFS = (
    "示例建设公司甲（虚构）",
    "示例产业公司乙（虚构）",
    "示例供应链公司丙（虚构）",
)
FICTIONAL_CASE_DEFENDANTS = tuple(
    f"示例被告公司{index:02d}（虚构）"
    for index in range(1, 13)
)
FICTIONAL_TRAVEL_DESTINATIONS = (
    "示例城市甲",
    "示例城市乙",
    "示例城市甲",
)
ORIGIN_STATUSES = {
    "system_fact",
    "human_record",
    "imported_record",
    "ai_extracted",
    "ai_summary",
    "ai_inference",
    "ai_suggestion",
    "external_clue",
    "unknown",
    "conflicted",
}
REQUIRED_ROLE_IDS = {
    "legal_member",
    "team_lead",
    "department_head",
    "legal_center_manager",
    "tenant_admin",
    "sandbox_admin",
    "system_admin",
}


def build_phase0_seed(manifest_path: str | Path = FIXTURE_PATH) -> dict[str, Any]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    snapshot: dict[str, Any] = {
        "metadata": {
            "seed_id": manifest["seed_id"],
            "schema_version": 1,
            "generator_revision": SEED_GENERATOR_REVISION,
            "fixture": True,
            "fixture_notice": "Fictional public examples only; not production business facts.",
            "as_of": manifest["as_of"],
            "origin_statuses": sorted(ORIGIN_STATUSES),
        },
        "tenants": [],
        "companies": [],
        "departments": [],
        "teams": [],
        "users": [],
        "roles": [],
        "sources": [],
        "metric_definitions": [],
        "cases": [],
        "daily_submissions": [],
        "weekly_submissions": [],
        "monthly_submissions": [],
        "performance_metrics": [],
        "target_collections": [],
        "report_runs": [],
        "manual_metric_entries": [],
        "travels": [],
        "quality_issues": [],
    }
    for tenant_config in manifest["tenants"]:
        _add_tenant(snapshot, tenant_config, manifest["as_of"])
    return snapshot


def _add_tenant(snapshot: dict[str, Any], config: dict[str, Any], as_of: str) -> None:
    tenant_id = config["id"]
    company = config["company"]
    department = config["department"]
    snapshot["tenants"].append(
        {
            "id": tenant_id,
            "name": config["name"],
            "status": "sandbox",
            "fixture": True,
            "default_company_id": company["id"],
        }
    )
    snapshot["companies"].append({**company, "tenant_id": tenant_id, "fixture": True})
    snapshot["departments"].append(
        {**department, "tenant_id": tenant_id, "company_id": company["id"], "fixture": True}
    )
    for team in config["teams"]:
        snapshot["teams"].append(
            {
                **team,
                "tenant_id": tenant_id,
                "company_id": company["id"],
                "department_id": department["id"],
                "fixture": True,
            }
        )
    role_ids: set[str] = set()
    for user in config["users"]:
        snapshot["users"].append(
            {
                **user,
                "tenant_id": tenant_id,
                "company_id": company["id"],
                "department_id": department["id"],
                "fixture": True,
            }
        )
        role_ids.update(user["roles"])
    role_ids.update(REQUIRED_ROLE_IDS)
    for role_id in sorted(role_ids):
        snapshot["roles"].append(
            {
                "id": f"{tenant_id}-{role_id}",
                "tenant_id": tenant_id,
                "code": role_id,
                "name": role_id.replace("_", " ").title(),
                "permissions": _role_permissions(role_id),
                "fixture": True,
            }
        )
    source_ids = _add_sources(snapshot, config, as_of)
    _add_metric_definitions(snapshot, config, source_ids)
    _add_cases(snapshot, config, source_ids, as_of)
    _add_period_submissions(snapshot, config, source_ids, as_of)
    _add_performance_reporting(snapshot, config, source_ids, as_of)
    _add_travels(snapshot, config, source_ids)
    _add_quality_issues(snapshot, config, source_ids)


def _role_permissions(role_id: str) -> list[str]:
    mapping = {
        "tenant_admin": ["read:all", "sandbox:reset", "permission:inspect"],
        "tenant_reader": ["read:tenant", "source:inspect"],
        "case_owner": ["read:team", "read:assigned_case"],
        "team_lead": ["read:team", "metric:inspect"],
        "legal_member": ["read:self", "read:assigned_case"],
        "department_head": ["read:department", "metric:inspect", "source:inspect"],
        "legal_center_manager": ["read:legal_center", "metric:inspect", "source:inspect", "export:scoped"],
        "sandbox_admin": ["read:all", "sandbox:reset", "source:inspect"],
        "system_admin": ["system:inspect", "tenant:provision"],
    }
    return mapping.get(role_id, ["read:self"])


def _add_sources(
    snapshot: dict[str, Any], config: dict[str, Any], as_of: str
) -> dict[str, str]:
    tenant_id = config["id"]
    definitions = [
        ("case", "case_registry_adapter", "案件台账适配器", "imported_record"),
        ("daily", "daily_form_adapter", "日报表单适配器", "human_record"),
        ("weekly", "weekly_form_adapter", "周报表单独立适配器", "human_record"),
        ("monthly", "monthly_form_adapter", "月报表单独立适配器", "human_record"),
        ("travel", "travel_form_adapter", "出差登记适配器", "human_record"),
        ("ai", "sandbox_ai_adapter", "Sandbox AI 派生适配器", "ai_summary"),
        ("external", "external_clue_adapter", "外部线索适配器", "external_clue"),
    ]
    ids: dict[str, str] = {}
    for key, adapter, name, origin_status in definitions:
        source_id = f"{tenant_id}-source-{key}"
        ids[key] = source_id
        is_periodic = key in {"daily", "weekly", "monthly"}
        snapshot["sources"].append(
            {
                "id": source_id,
                "source_id": source_id,
                "tenant_id": tenant_id,
                "name": name,
                "source_name": name,
                "business_domain": {
                    "case": "case_management",
                    "daily": "daily_reporting",
                    "weekly": "weekly_reporting",
                    "monthly": "monthly_reporting",
                    "travel": "travel_management",
                    "ai": "ai_derived_content",
                    "external": "external_clues",
                }[key],
                "source_type": "sandbox_fixture_adapter",
                "table_or_endpoint": {
                    "case": "cases",
                    "daily": "daily_submissions",
                    "weekly": "weekly_submissions",
                    "monthly": "monthly_submissions",
                    "travel": "travels",
                    "ai": "lifecycle.origin",
                    "external": "lifecycle.external_clue",
                }[key],
                "adapter": adapter,
                "recommended_adapter": adapter,
                "status": "sandbox_fixture",
                "mode": "read_only",
                "origin_status": origin_status,
                "primary_key": "id",
                "tenant_field": "tenant_id",
                "company_field": "company_id",
                "department_field": "department_id" if key in {"case", "daily", "weekly", "monthly"} else None,
                "team_field": "team_id" if key in {"case", "daily", "weekly", "monthly", "travel"} else None,
                "person_field": "user_id" if is_periodic else ("traveler_user_id" if key == "travel" else None),
                "case_field": "case_id" if key in {"case", "travel", "external"} else "linked_case_ids" if is_periodic else None,
                "period_field": "period" if is_periodic else None,
                "time_fields": ["period"] if is_periodic else ["refreshed_at"],
                "owner": "Legal Operations Sandbox",
                "data_owner": "Legal Operations Sandbox",
                "quality_rules": ["tenant_required", "source_reference_required", "fixture_flag_required"],
                "data_quality": {
                    "status": "fixture_only",
                    "rules": ["tenant_required", "source_reference_required", "fixture_flag_required"],
                },
                "sensitivity": "internal_sandbox",
                "known_gaps": ["real_source_not_connected"],
                "update_method": "tenant_scoped_seed_or_read_only_adapter",
                "update_frequency": "on_demand",
                "sandbox_usable": True,
                "drilldown_supported": True,
                "schema_version": 1,
                "schema": {
                    "version": 1,
                    "required": ["id", "tenant_id", "fixture"],
                    "additional_properties": True,
                },
                "refreshed_at": f"{as_of}T09:00:00+08:00",
                "fixture": True,
            }
        )
    return ids


def _add_metric_definitions(
    snapshot: dict[str, Any], config: dict[str, Any], source_ids: dict[str, str]
) -> None:
    tenant_id = config["id"]
    company_id = config["company"]["id"]
    definitions = [
        ("open_case_count", "在办案件数", "件", "confirmed", "case"),
        ("high_risk_case_count", "高风险案件数", "件", "confirmed", "case"),
        ("active_travel_count", "进行中出差数", "次", "confirmed", "travel"),
        ("daily_submission_rate", "日报提交率", "%", "confirmed", "daily"),
        ("risk_resolution_rate", "风险化解率", "%", "draft", "case"),
        ("case_efficiency_index", "案件效率指数", "分", "pending_business_confirmation", "case"),
    ]
    for code, name, unit, status, source_key in definitions:
        snapshot["metric_definitions"].append(
            {
                "id": f"{tenant_id}-metric-{code}",
                "tenant_id": tenant_id,
                "company_id": company_id,
                "code": code,
                "name": name,
                "unit": unit,
                "definition_status": status,
                "registry_key": code if status == "confirmed" else None,
                "source_id": source_ids[source_key],
                "source_component": source_key,
                "owner": "Sandbox metric registry",
                "fixture": True,
            }
        )


def _add_cases(
    snapshot: dict[str, Any], config: dict[str, Any], source_ids: dict[str, str], as_of: str
) -> None:
    tenant_id = config["id"]
    company_id = config["company"]["id"]
    department_id = config["department"]["id"]
    teams = config["teams"]
    users = config["users"]
    phases = ["intake", "filing", "trial", "execution", "closure"]
    statuses = ["open", "open", "open", "monitoring", "closed", "open"]
    risk_levels = ["high", "medium", "low", "medium", "low", "high"]
    plaintiffs = FICTIONAL_CASE_PLAINTIFFS
    defendants = FICTIONAL_CASE_DEFENDANTS
    causes = ["建设工程施工合同纠纷", "买卖合同纠纷", "服务合同纠纷", "执行异议", "仲裁保全", "应收账款催收"]
    for index in range(1, int(config["case_count"]) + 1):
        case_id = f"{tenant_id.removeprefix('sandbox-')}-case-{index:02d}"
        team = teams[(index - 1) % len(teams)]
        owner = users[(index - 1) % len(users)]
        phase = phases[(index - 1) % len(phases)]
        status = statuses[(index - 1) % len(statuses)]
        risk = risk_levels[(index - 1) % len(risk_levels)]
        snapshot["cases"].append(
            {
                "id": case_id,
                "tenant_id": tenant_id,
                "company_id": company_id,
                "department_id": department_id,
                "team_id": team["id"],
                "owner_user_id": owner["id"],
                "title": f"{plaintiffs[(index - 1) % len(plaintiffs)]}诉{defendants[(index - 1) % len(defendants)]}",
                "plaintiff": plaintiffs[(index - 1) % len(plaintiffs)],
                "defendants": [defendants[(index - 1) % len(defendants)]],
                "case_number": f"EXAMPLE-CASE-2026-{index:04d}",
                "cause": causes[(index - 1) % len(causes)],
                "owner_name": owner.get("name", owner["id"]),
                "last_progress": f"2026-07-{max(1, 12 - index):02d}",
                "stagnation_days": index * 3 if status != "closed" else 0,
                "execution_status": "已查控待反馈" if phase == "execution" else "尚未进入执行",
                "recovered_amount": index * 20000 if phase in {"execution", "closure"} else 0,
                "supported_amount": 160000 + index * 30000,
                "next_action": ["补充财产线索", "确认开庭材料", "跟进法院反馈", "核对回款流水"][index % 4],
                "case_type": ["诉讼", "仲裁", "执行", "非诉争议"][index % 4],
                "status": status,
                "current_phase": phase,
                "risk_level": risk,
                "amount": 180000 + index * 37500,
                "currency": "CNY",
                "fixture": True,
                "origin": _origin("imported_record", source_ids["case"], confirmed=True),
                "lifecycle": _case_lifecycle(
                    tenant_id=tenant_id,
                    case_id=case_id,
                    current_phase=phase,
                    source_ids=source_ids,
                    as_of=as_of,
                    index=index,
                ),
            }
        )


def _case_lifecycle(
    *,
    tenant_id: str,
    case_id: str,
    current_phase: str,
    source_ids: dict[str, str],
    as_of: str,
    index: int,
) -> dict[str, Any]:
    lanes = [
        {"id": "intake", "name": "收件与评估"},
        {"id": "filing", "name": "立案与保全"},
        {"id": "trial", "name": "审理与裁判"},
        {"id": "execution", "name": "执行与回款"},
        {"id": "closure", "name": "结案与复盘"},
    ]
    specs = [
        ("event", "intake", "案件进入台账", "system_fact", "case", True),
        ("party", "intake", "当事人关系已登记", "human_record", "case", True),
        ("document", "intake", "基础证据目录", "imported_record", "case", True),
        ("work_record", "filing", "内部研判记录", "human_record", "case", True),
        ("event", "filing", "立案节点", "system_fact", "case", True),
        ("asset", "filing", "财产线索待核验", "external_clue", "external", False),
        ("document", "trial", "庭审材料目录", "imported_record", "case", True),
        ("event", "trial", "裁判节点", "system_fact", "case", True),
        ("risk", "trial", "期限风险摘要", "ai_summary", "ai", False),
        ("task", "trial", "复核关键日期", "ai_suggestion", "ai", False),
        ("event", "execution", "执行立案节点", "system_fact", "case", True),
        ("collection", "execution", "回款计划记录", "human_record", "case", True),
        ("external_clue", "execution", "外部资产线索", "external_clue", "external", False),
        ("event", "closure", "结案审批节点", "system_fact", "case", True),
        ("work_record", "closure", "结案复盘草稿", "ai_inference", "ai", False),
        ("risk", "closure", "金额口径存在冲突", "conflicted", "case", False),
    ]
    node_limits = [16, 15, 14, 13, 12, 11, 10]
    selected_specs = specs[:node_limits[(index - 1) % len(node_limits)]]
    selected_specs.extend(spec for spec in specs if spec[0] == "event" and spec not in selected_specs)
    specs = selected_specs
    nodes = []
    for position, (kind, lane, title, status, source_key, confirmed) in enumerate(specs, start=1):
        nodes.append(
            {
                "id": f"{case_id}-node-{position:02d}",
                "tenant_id": tenant_id,
                "case_id": case_id,
                "parent_id": None if position == 1 else f"{case_id}-node-01",
                "lane_id": lane,
                "kind": kind,
                "title": title,
                "occurred_at": f"{as_of}T{min(8 + position, 23):02d}:00:00+08:00",
                "state": "current" if lane == current_phase else "recorded",
                "origin": _origin(status, source_ids[source_key], confirmed=confirmed),
                "fixture": True,
                "sequence": index * 100 + position,
            }
        )
    return {
        "root_id": f"{case_id}-node-01",
        "lanes": lanes,
        "nodes": nodes,
        "timeline_node_ids": [node["id"] for node in nodes if node["kind"] == "event"],
    }


def _add_period_submissions(
    snapshot: dict[str, Any], config: dict[str, Any], source_ids: dict[str, str], as_of: str
) -> None:
    tenant_id = config["id"]
    company_id = config["company"]["id"]
    _add_daily_history(snapshot, config, source_ids, as_of)
    for period, collection, source_key, adapter in (
        ("weekly", "weekly_submissions", "weekly", "weekly_form_adapter"),
        ("monthly", "monthly_submissions", "monthly", "monthly_form_adapter"),
    ):
        for index, user in enumerate(config["users"], start=1):
            tenant_prefix = tenant_id.removeprefix("sandbox-")
            linked_case_id = f"{tenant_prefix}-case-{((index - 1) % int(config['case_count'])) + 1:02d}"
            snapshot[collection].append(
                {
                    "id": f"{tenant_prefix}-{period}-{index:02d}",
                    "source_id": f"{tenant_id}-{period}-submission-{index:02d}",
                    "tenant_id": tenant_id,
                    "company_id": company_id,
                    "team_id": user["team_id"],
                    "user_id": user["id"],
                    "period": as_of if period == "daily" else ("2026-W28" if period == "weekly" else "2026-07"),
                    "status": "completed" if index % 4 else "pending",
                    "summary": f"{period} 独立表单的 Sandbox 提交 {index}",
                    "raw_text": f"{period} 原始 Sandbox 表单文本 {index}；仅用于端到端演示。",
                    "linked_case_ids": [linked_case_id],
                    "adapter": adapter,
                    "origin": _origin("human_record", source_ids[source_key], confirmed=True),
                    "fixture": True,
                }
            )


def _add_daily_history(
    snapshot: dict[str, Any], config: dict[str, Any], source_ids: dict[str, str], as_of: str
) -> None:
    tenant_id = config["id"]
    company_id = config["company"]["id"]
    tenant_prefix = tenant_id.removeprefix("sandbox-")
    end_date = date.fromisoformat(as_of)
    work_templates = (
        (
            "完成示例案件甲（虚构）的财产查控材料复核，并联系示例法院确认下周反馈安排。",
            "整理查控材料并跟进法院",
            "等待法院反馈后更新执行方案",
            "案件推进",
        ),
        (
            "审阅采购框架协议第 8—12 条，标记付款、验收和违约责任三处风险，并向业务部门反馈修改意见。",
            "审阅采购框架协议并出具风险意见",
            "与采购团队确认违约责任条款",
            "合同审核",
        ),
        (
            "汇总本周各组目标完成情况，发现争议解决组两项指标缺少实际值，已提醒负责人补充。",
            "核对团队绩效指标并发起补充提醒",
            "完成周度绩效报告初稿",
            "运营管理",
        ),
        (
            "参加示例案件乙（虚构）的庭前会议，对方提出分期调解方案；已记录要点并安排明日与业务负责人评估。",
            "参加庭前会议并记录调解方案",
            "评估分期方案及担保条件",
            "庭审与调解",
        ),
        (
            "赴示例城市甲处理两起虚构执行案件，现场调取示例卷宗并与同事合并安排法院沟通，减少一次重复行程。",
            "示例城市甲出差处理虚构执行案件并完成协同",
            "整理出差材料并关联案件进展",
            "出差办案",
        ),
        (
            "补充一笔虚构的 12 万元回款记录，核对示例流水后将原摘要从“已确认”改为“待财务复核”。",
            "补充回款记录并修订确认状态",
            "等待财务复核到账信息",
            "执行回款",
        ),
    )
    history_days = int(config.get("daily_history_days", 3))
    for day_offset in range(history_days):
        report_date = end_date - timedelta(days=day_offset)
        for index, user in enumerate(config["users"], start=1):
            case_number = ((index + day_offset - 1) % int(config["case_count"])) + 1
            linked_case_id = f"{tenant_prefix}-case-{case_number:02d}"
            raw_text, work_item, tomorrow_plan, work_type = work_templates[
                (index + day_offset - 1) % len(work_templates)
            ]
            is_current = day_offset == 0
            submission_id = (
                f"{tenant_prefix}-daily-{index:02d}"
                if is_current
                else f"{tenant_prefix}-daily-{report_date:%Y%m%d}-{index:02d}"
            )
            status = "pending" if is_current and index == len(config["users"]) else "completed"
            revisions = []
            deleted_items = []
            if day_offset == 1 and index == 2:
                revisions = [
                    {
                        "at": f"{report_date}T18:22:00+08:00",
                        "operator_name": user["name"],
                        "action": "修改",
                        "before": "等待法院下周反馈",
                        "after": "法院预计本周五反馈查控结果",
                    }
                ]
            if day_offset == 2 and index == 3:
                deleted_items = [
                    {
                        "text": "参加例行会议",
                        "reason": "重复记录，已作废",
                        "deleted_at": f"{report_date}T17:45:00+08:00",
                    }
                ]
            snapshot["daily_submissions"].append(
                {
                    "id": submission_id,
                    "source_id": f"{tenant_id}-daily-submission-{report_date:%Y%m%d}-{index:02d}",
                    "tenant_id": tenant_id,
                    "company_id": company_id,
                    "team_id": user["team_id"],
                    "user_id": user["id"],
                    "user_name": user["name"],
                    "period": report_date.isoformat(),
                    "status": status,
                    "summary": work_item,
                    "raw_text": raw_text,
                    "work_items": [
                        {
                            "id": f"{submission_id}-item-01",
                            "text": work_item,
                            "work_type": work_type,
                            "linked_case_id": linked_case_id,
                            "status": "有效",
                        }
                    ],
                    "tomorrow_plan": tomorrow_plan,
                    "linked_case_ids": [linked_case_id],
                    "linked_travel_ids": (
                        [f"{tenant_id}-travel-01"] if work_type == "出差办案" else []
                    ),
                    "revisions": revisions,
                    "deleted_items": deleted_items,
                    "submission_status_label": "已提交" if status == "completed" else "待补充",
                    "ai_split_status": "人工已确认" if status == "completed" else "待本人确认",
                    "source_display": "来源：日报机器人",
                    "adapter": "daily_form_adapter",
                    "origin": _origin("human_record", source_ids["daily"], confirmed=status == "completed"),
                    "fixture": True,
                }
            )


def _add_performance_reporting(
    snapshot: dict[str, Any], config: dict[str, Any], source_ids: dict[str, str], as_of: str
) -> None:
    tenant_id = config["id"]
    company_id = config["company"]["id"]
    metric_specs = (
        ("case_progress_rate", "重点案件按期推进率", "%", "workbuddy_skill", "案件推进 Skill", 0.9, (0.94, 0.81, 0.88)),
        ("contract_review_sla", "合同审核按时完成率", "%", "workbuddy_skill", "合同 SLA Skill", 0.95, (0.97, 0.93, 0.96)),
        ("recovery_amount", "执行回款金额", "元", "manual", "法务运营人工维护", 1500000, (620000, 480000, 120000)),
        ("risk_closed", "风险事项关闭数", "项", "robot_collection", "负责人目标收集机器人", 12, (5, 3, 2)),
    )
    for period_type, period_start, period_end in (
        ("weekly", "2026-07-06", "2026-07-12"),
        ("monthly", "2026-07-01", "2026-07-31"),
    ):
        for spec_index, (code, name, unit, source_type, source_name, target, values) in enumerate(metric_specs):
            for team_index, team in enumerate(config["teams"]):
                actual = values[team_index % len(values)]
                completion = actual / target if target else 0
                snapshot["performance_metrics"].append(
                    {
                        "id": f"{tenant_id}-{period_type}-{code}-{team_index + 1}",
                        "tenant_id": tenant_id,
                        "company_id": company_id,
                        "department_id": config["department"]["id"],
                        "team_id": team["id"],
                        "team_name": team["name"],
                        "metric_code": code,
                        "metric_name": name,
                        "metric_category": "案件与运营" if spec_index % 2 == 0 else "质量与效率",
                        "period_type": period_type,
                        "period_start": period_start,
                        "period_end": period_end,
                        "owner": f"{team['name']}负责人",
                        "target_value": target,
                        "actual_value": actual,
                        "completion_rate": round(completion, 4),
                        "unit": unit,
                        "data_source_type": source_type,
                        "data_source_name": source_name,
                        "calculation_version": "2026.07-v1" if source_type == "workbuddy_skill" else "人工口径-v1",
                        "maintenance_status": "无需维护" if source_type == "workbuddy_skill" else "已维护",
                        "confirmation_status": "已确认" if completion >= 0.8 else "待负责人说明",
                        "updated_at": f"{as_of}T09:00:00+08:00",
                        "remark": "Sandbox 绩效演示数据",
                        "fixture": True,
                    }
                )
        snapshot["report_runs"].append(
            {
                "id": f"{tenant_id}-{period_type}-report-202607",
                "tenant_id": tenant_id,
                "period_type": period_type,
                "title": "法务中心周度绩效报告" if period_type == "weekly" else "法务中心月度绩效报告",
                "period_start": period_start,
                "period_end": period_end,
                "status": "待管理人员确认" if period_type == "weekly" else "报告预览已生成",
                "overall_summary": "重点案件推进总体平稳，合同治理组目标填报与执行回款数据仍需补充说明。",
                "risk_summary": "3 个案件超过 21 天无实质进展；1 项回款数据等待财务复核。",
                "coordination_needed": "协调合同治理组负责人完成月度目标回复，并确认示例城市甲同期出差安排。",
                "next_period_goal": "重点案件按期推进率达到 90%，完成执行回款 150 万元。",
                "generated_at": f"{as_of}T10:30:00+08:00",
                "fixture": True,
            }
        )
    for index, team in enumerate(config["teams"]):
        status = ("已确认", "未回复", "已回复")[index % 3]
        snapshot["target_collections"].append(
            {
                "id": f"{tenant_id}-target-collection-{index + 1}",
                "tenant_id": tenant_id,
                "team_id": team["id"],
                "team_name": team["name"],
                "leader": f"{team['name']}负责人",
                "question_sent_at": "2026-07-08T09:00:00+08:00",
                "reply_at": None if status == "未回复" else "2026-07-08T15:20:00+08:00",
                "target_value": None if status == "未回复" else 90 + index,
                "actual_value": None if status == "未回复" else 82 + index * 3,
                "reason": "重点案件节点延期" if index == 0 else "",
                "risk": "执行回款尚待复核" if index == 1 else "",
                "coordination_needed": "需协调法院沟通时间" if index == 0 else "",
                "next_goal": "下周期完成全部目标数据确认" if status != "未回复" else "",
                "reminder_status": "已提醒 1 次" if status == "未回复" else "无需提醒",
                "confirmation_status": status,
                "fixture": True,
            }
        )
        snapshot["manual_metric_entries"].append(
            {
                "id": f"{tenant_id}-manual-recovery-{index + 1}",
                "tenant_id": tenant_id,
                "team_id": team["id"],
                "metric_name": "执行回款金额",
                "maintainer": "法务运营专员",
                "maintained_at": f"{as_of}T11:00:00+08:00",
                "target_value": 500000,
                "actual_value": (420000, 360000, 120000)[index % 3],
                "remark": "银行流水已核对" if index != 2 else "等待财务复核",
                "status": "已确认" if index != 2 else "待确认",
                "history": [{"at": f"{as_of}T10:30:00+08:00", "action": "录入目标与实际值"}],
                "fixture": True,
            }
        )


def _add_travels(snapshot: dict[str, Any], config: dict[str, Any], source_ids: dict[str, str]) -> None:
    tenant_id = config["id"]
    for index, user in enumerate(config["users"][:3], start=1):
        case_number = ((index - 1) % int(config["case_count"])) + 1
        case_id = f"{tenant_id.removeprefix('sandbox-')}-case-{case_number:02d}"
        snapshot["travels"].append(
            {
                "id": f"{tenant_id}-travel-{index:02d}",
                "tenant_id": tenant_id,
                "company_id": config["company"]["id"],
                "team_id": user["team_id"],
                "traveler_user_id": user["id"],
                "case_id": case_id,
                "destination": FICTIONAL_TRAVEL_DESTINATIONS[index - 1],
                "start_date": ["2026-07-11", "2026-07-12", "2026-07-12"][index - 1],
                "end_date": ["2026-07-12", "2026-07-13", "2026-07-14"][index - 1],
                "status": "active" if index < 3 else "planned",
                "purpose": ["赴法院调取执行卷宗", "参加庭前会议并核对证据", "走访被执行人财产线索"][index - 1],
                "origin": _origin("human_record", source_ids["travel"], confirmed=True),
                "fixture": True,
            }
        )


def _add_quality_issues(snapshot: dict[str, Any], config: dict[str, Any], source_ids: dict[str, str]) -> None:
    tenant_id = config["id"]
    case_prefix = tenant_id.removeprefix("sandbox-")
    snapshot["quality_issues"].extend(
        [
            {
                "id": f"{tenant_id}-quality-01",
                "tenant_id": tenant_id,
                "severity": "warning",
                "resource_type": "case",
                "resource_id": f"{case_prefix}-case-01",
                "code": "AI_FIELD_UNCONFIRMED",
                "message": "AI 摘要尚未人工确认。",
                "source_id": source_ids["ai"],
                "fixture": True,
            },
            {
                "id": f"{tenant_id}-quality-02",
                "tenant_id": tenant_id,
                "severity": "info",
                "resource_type": "source",
                "resource_id": source_ids["external"],
                "code": "EXTERNAL_CLUE_PENDING",
                "message": "外部线索仅作提示，不构成系统事实。",
                "source_id": source_ids["external"],
                "fixture": True,
            },
            {
                "id": f"{tenant_id}-quality-03",
                "tenant_id": tenant_id,
                "severity": "warning",
                "resource_type": "case",
                "resource_id": f"{case_prefix}-case-01",
                "code": "CONFLICTED_AMOUNT",
                "message": "两个导入字段的金额口径冲突，等待人工裁决。",
                "source_id": source_ids["case"],
                "fixture": True,
            },
        ]
    )


def _origin(status: str, source_id: str, *, confirmed: bool) -> dict[str, Any]:
    if status not in ORIGIN_STATUSES:
        raise ValueError(f"unsupported origin status: {status}")
    is_ai = status.startswith("ai_")
    return {
        "status": status,
        "content_origin": status,
        "source_id": source_id,
        "source_type": "sandbox_source_reference",
        "generated_at": "2026-07-11T09:00:00+08:00",
        "generator": "sandbox-fixture-generator-v1" if is_ai else None,
        "generated_by": "sandbox-fixture-generator-v1" if is_ai else None,
        "confidence": 0.72 if is_ai else None,
        "confirmed": bool(confirmed),
        "confirmation_status": "confirmed" if confirmed else "pending_confirmation",
        "reviewer_user_id": None,
        "reviewed_by": None,
        "reviewed_at": None,
    }
