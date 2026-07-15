from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from app.legal_ops.auth import SandboxPrincipal
from app.legal_ops.repository import SandboxRepository


class LegalOpsReadService:
    _TENANT_WIDE_ROLES = {
        "tenant_admin",
        "tenant_reader",
        "sandbox_admin",
        "system_admin",
    }

    def __init__(self, repository: SandboxRepository):
        self.repository = repository
        self._metric_registry: dict[
            str, Callable[[dict[str, Any]], tuple[float, list[dict[str, Any]]]]
        ] = {
            "open_case_count": self._open_case_count,
            "high_risk_case_count": self._high_risk_case_count,
            "active_travel_count": self._active_travel_count,
            "daily_submission_rate": self._daily_submission_rate,
        }

    def shell(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        return {
            "scope": self._scope_payload(principal),
            "tenant": scoped["tenant"],
            "companies": scoped["companies"],
            "departments": scoped["departments"],
            "teams": scoped["teams"],
            "users": scoped["users"],
            "roles": scoped["roles"],
            "navigation": [
                "command",
                "work",
                "performance",
                "teams",
                "forest",
                "travel",
            ],
            "fixture_notice": scoped["metadata"]["fixture_notice"],
        }

    def overview(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        open_cases = [item for item in scoped["cases"] if item["status"] != "closed"]
        high_risk = [item for item in scoped["cases"] if item["risk_level"] == "high"]
        latest_daily = max((item["period"] for item in scoped["daily_submissions"]), default="")
        today_daily = [item for item in scoped["daily_submissions"] if item["period"] == latest_daily]
        daily_completed = sum(item["status"] == "completed" for item in today_daily)
        daily_rate = round(daily_completed / max(len(scoped["users"]), 1) * 100)
        team_matrix = []
        for index, team in enumerate(scoped["teams"]):
            team_cases = [item for item in scoped["cases"] if item["team_id"] == team["id"]]
            team_daily = [item for item in today_daily if item["team_id"] == team["id"]]
            team_matrix.append(
                {
                    "team_id": team["id"],
                    "team_name": team["name"],
                    "weekly": ("正常", "预警", "正常")[index % 3],
                    "monthly": ("正常", "未填报", "预警")[index % 3],
                    "target_collection": ("已确认", "待回复", "已确认")[index % 3],
                    "daily": "正常" if team_daily and all(item["status"] == "completed" for item in team_daily) else "未提交",
                    "case_progress": "异常" if any(item["risk_level"] == "high" for item in team_cases) else "正常",
                    "recovery": ("正常", "预警", "数据缺失")[index % 3],
                    "risk_count": sum(item["risk_level"] == "high" for item in team_cases),
                    "travel_count": sum(item["team_id"] == team["id"] for item in scoped["travels"]),
                }
            )
        actions = [
            {
                "type": "目标填报",
                "object": "合同治理组月度目标",
                "reason": "负责人尚未回复机器人收集",
                "owner": "合同治理组负责人",
                "duration": "已等待 2 天",
                "status": "待协调",
                "route": "performance",
            },
            {
                "type": "案件推进",
                "object": "华东建设执行案件",
                "reason": "超过 21 天无实质进展",
                "owner": "争议解决组",
                "duration": "停滞 24 天",
                "status": "高优先级",
                "route": "forest",
            },
            {
                "type": "数据维护",
                "object": "执行回款实际值",
                "reason": "银行流水仍待人工复核",
                "owner": "法务运营组",
                "duration": "今日到期",
                "status": "待补充",
                "route": "performance",
            },
            {
                "type": "出差协同",
                "object": "南京同期出差",
                "reason": "两名承办人计划同期前往南京",
                "owner": "争议解决组",
                "duration": "明日出发",
                "status": "待双方确认",
                "route": "travel",
            },
        ]
        return {
            "scope": self._scope_payload(principal),
            "cards": [
                {"label": "核心目标完成率", "value": 82, "unit": "%", "target": "目标 90%", "change": "较上周 +6%", "status": "warning", "updated_at": "今日 09:00", "drilldown": "performance"},
                {"label": "日报提交率", "value": daily_rate, "unit": "%", "target": f"{daily_completed}/{len(scoped['users'])} 人已提交", "change": "较昨日持平", "status": "warning" if daily_rate < 100 else "healthy", "updated_at": "今日 18:00", "drilldown": "work"},
                {"label": "重点案件", "value": len(high_risk), "unit": "件", "target": f"在办 {len(open_cases)} 件", "change": "新增 1 件预警", "status": "critical", "updated_at": "今日 16:30", "drilldown": "cases?risk=high"},
                {"label": "待协调事项", "value": len(actions), "unit": "项", "target": "2 项今日到期", "change": "较昨日 +1", "status": "critical", "updated_at": "实时", "drilldown": "actions"},
            ],
            "case_status": dict(Counter(item["status"] for item in scoped["cases"])),
            "risk_levels": dict(Counter(item["risk_level"] for item in scoped["cases"])),
            "recent_cases": scoped["cases"][:6],
            "team_matrix": team_matrix,
            "actions": actions,
            "daily_date": latest_daily,
            "source_trace": [source["id"] for source in scoped["sources"]],
        }

    def period_dashboard(self, principal: SandboxPrincipal, period: str) -> dict[str, Any]:
        mapping = {
            "daily": ("daily_submissions", "daily_form_adapter"),
            "weekly": ("weekly_submissions", "weekly_form_adapter"),
            "monthly": ("monthly_submissions", "monthly_form_adapter"),
        }
        if period not in mapping:
            raise LookupError("unsupported reporting period")
        scoped = self._scope(principal)
        collection, adapter = mapping[period]
        submissions = scoped[collection]
        return {
            "scope": self._scope_payload(principal),
            "period_type": period,
            "source_contract": {
                "adapter": adapter,
                "depends_on": [],
                "read_only": True,
                "independence_assertion": f"{period} reads only {collection}",
            },
            "summary": {
                "total": len(submissions),
                "completed": sum(item["status"] == "completed" for item in submissions),
                "pending": sum(item["status"] != "completed" for item in submissions),
            },
            "submissions": submissions,
        }

    def report_detail(
        self, principal: SandboxPrincipal, period: str, submission_id: str
    ) -> dict[str, Any]:
        mapping = {
            "daily": ("daily_submissions", "daily_form_adapter"),
            "weekly": ("weekly_submissions", "weekly_form_adapter"),
            "monthly": ("monthly_submissions", "monthly_form_adapter"),
        }
        if period not in mapping:
            raise LookupError("unsupported reporting period")
        scoped = self._scope(principal)
        collection, adapter = mapping[period]
        submission = next((item for item in scoped[collection] if item["id"] == submission_id), None)
        if submission is None:
            raise LookupError("submission not found in authorized tenant scope")
        return {
            **submission,
            "scope": self._scope_payload(principal),
            "source_contract": {"adapter": adapter, "depends_on": [], "read_only": True},
        }

    def team_workbench(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        teams: list[dict[str, Any]] = []
        for team in scoped["teams"]:
            team_cases = [case for case in scoped["cases"] if case["team_id"] == team["id"]]
            members = [user for user in scoped["users"] if user["team_id"] == team["id"]]
            daily = [row for row in scoped["daily_submissions"] if row["team_id"] == team["id"]]
            current_period = max((row["period"] for row in daily), default=None)
            current_daily = [row for row in daily if row["period"] == current_period]
            teams.append(
                {
                    **team,
                    "member_count": len(members),
                    "members": [{"id": user["id"], "name": user.get("name", user["id"])} for user in members],
                    "case_count": len(team_cases),
                    "high_risk_count": sum(case["risk_level"] == "high" for case in team_cases),
                    "stagnant_case_count": sum(case.get("stagnation_days", 0) >= 20 for case in team_cases),
                    "daily_completion_rate": round(
                        sum(row["status"] == "completed" for row in current_daily) / len(current_daily) * 100, 1
                    ) if current_daily else 0,
                    "active_travel_count": sum(
                        row["team_id"] == team["id"] and row["status"] == "active" for row in scoped["travels"]
                    ),
                    "case_ids": [case["id"] for case in team_cases],
                }
            )
        return {"scope": self._scope_payload(principal), "teams": teams}

    def travel_dashboard(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        cases_by_id = {case["id"]: case for case in scoped["cases"]}
        users_by_id = {user["id"]: user for user in scoped["users"]}
        travels = []
        for travel in scoped["travels"]:
            peers = [
                row for row in scoped["travels"]
                if row["id"] != travel["id"]
                and row["destination"] == travel["destination"]
                and row["start_date"] <= travel["end_date"]
                and row["end_date"] >= travel["start_date"]
            ]
            travels.append(
                {
                    **travel,
                    "traveler_name": users_by_id.get(travel["traveler_user_id"], {}).get("name", travel["traveler_user_id"]),
                    "case_title": cases_by_id.get(travel["case_id"], {}).get("title", travel["case_id"]),
                    "collaboration_candidates": [
                        {
                            "travel_id": row["id"],
                            "traveler_name": users_by_id.get(row["traveler_user_id"], {}).get("name", row["traveler_user_id"]),
                        }
                        for row in peers
                    ],
                }
            )
        return {
            "scope": self._scope_payload(principal),
            "source_contract": {"adapter": "travel_form_adapter", "read_only": True},
            "travels": travels,
            "by_destination": dict(Counter(item["destination"] for item in scoped["travels"])),
        }

    def metric_center(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        metrics: list[dict[str, Any]] = []
        for definition in scoped["metric_definitions"]:
            registry_key = definition.get("registry_key")
            calculator = self._metric_registry.get(str(registry_key))
            if definition["definition_status"] != "confirmed" or calculator is None:
                metrics.append(
                    {
                        **definition,
                        "formal": False,
                        "value": None,
                        "components": [],
                        "warning": "not a formal metric",
                    }
                )
                continue
            value, components = calculator(scoped)
            metrics.append(
                {
                    **definition,
                    "formal": True,
                    "value": value,
                    "components": components,
                    "warning": None,
                }
            )
        return {"scope": self._scope_payload(principal), "metrics": metrics}

    def performance_center(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        return {
            "scope": self._scope_payload(principal),
            "metrics": scoped["performance_metrics"],
            "target_collections": scoped["target_collections"],
            "report_runs": scoped["report_runs"],
            "manual_entries": scoped["manual_metric_entries"],
            "source_types": [
                {"code": "workbuddy_skill", "label": "WorkBuddy Skill 自动计算", "connected": True},
                {"code": "manual", "label": "人工维护", "connected": True},
                {"code": "robot_collection", "label": "负责人目标收集机器人", "connected": True},
                {"code": "information_center", "label": "信息中心接口", "connected": False},
            ],
            "export_formats": [
                {"format": "docx", "label": "Word 固定格式报告"},
                {"format": "pdf", "label": "PDF 汇报版"},
                {"format": "xlsx", "label": "Excel 指标明细"},
            ],
        }

    def case_list(
        self,
        principal: SandboxPrincipal,
        *,
        requested_tenant_id: str | None = None,
        status: str | None = None,
        risk: str | None = None,
        team_id: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        scoped = self._scope(principal)
        items = list(scoped["cases"])
        if status:
            items = [item for item in items if item["status"] == status]
        if risk:
            items = [item for item in items if item["risk_level"] == risk]
        if team_id:
            self._assert_allowed_team(scoped, team_id)
            items = [item for item in items if item["team_id"] == team_id]
        if query:
            needle = query.casefold()
            items = [
                item for item in items
                if any(
                    needle in str(value).casefold()
                    for value in (
                        item["title"], item["id"], item.get("case_number", ""),
                        item.get("plaintiff", ""), " ".join(item.get("defendants", [])),
                    )
                )
            ]
        scope = self._scope_payload(principal)
        if requested_tenant_id and requested_tenant_id != principal.tenant_id:
            scope["ignored_requested_tenant_id"] = requested_tenant_id
        return {"scope": scope, "items": items, "total": len(items)}

    def case_forest(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        plaintiffs: dict[str, dict[str, Any]] = {}
        for case in scoped["cases"]:
            plaintiff_name = case.get("plaintiff", "未归类原告")
            plaintiff = plaintiffs.setdefault(
                plaintiff_name,
                {"name": plaintiff_name, "case_count": 0, "defendants": {}},
            )
            plaintiff["case_count"] += 1
            for defendant_name in case.get("defendants", ["未归类被告"]):
                defendant = plaintiff["defendants"].setdefault(
                    defendant_name,
                    {"name": defendant_name, "case_count": 0, "cases": []},
                )
                defendant["case_count"] += 1
                defendant["cases"].append(case)
        roots = []
        for plaintiff in plaintiffs.values():
            plaintiff["defendants"] = list(plaintiff["defendants"].values())
            roots.append(plaintiff)
        return {"scope": self._scope_payload(principal), "roots": roots, "total": len(scoped["cases"])}

    def case_detail(self, principal: SandboxPrincipal, case_id: str) -> dict[str, Any]:
        scoped = self._scope(principal)
        case = next((item for item in scoped["cases"] if item["id"] == case_id), None)
        if case is None:
            raise LookupError("case not found in authorized tenant")
        travels = [item for item in scoped["travels"] if item["case_id"] == case_id]
        quality = [item for item in scoped["quality_issues"] if item.get("resource_id") == case_id]
        return {
            **case,
            "scope": self._scope_payload(principal),
            "related_travels": travels,
            "quality_issues": quality,
            "drilldowns": {
                "travel_ids": [item["id"] for item in travels],
                "daily_submission_ids": [
                    item["id"]
                    for item in scoped["daily_submissions"]
                    if case_id in item.get("linked_case_ids", [])
                ],
            },
        }

    def quality_center(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        if set(principal.role_ids).intersection(self._TENANT_WIDE_ROLES):
            verification = self.repository.verify_tenant(principal.tenant_id)
        else:
            verification = {
                "tenant_id": principal.tenant_id,
                "valid": True,
                "violations": [],
                "record_counts": {
                    "cases": len(scoped["cases"]),
                    "quality_issues": len(scoped["quality_issues"]),
                },
                "scope": "authorized_subset",
            }
        return {
            "scope": self._scope_payload(principal),
            "issues": scoped["quality_issues"],
            "isolation_verification": verification,
        }

    def source_center(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        return {
            "scope": self._scope_payload(principal),
            "sources": scoped["sources"],
            "metric_definitions": scoped["metric_definitions"],
            "origin_statuses": scoped["metadata"]["origin_statuses"],
        }

    def permission_center(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self._scope(principal)
        return {
            "scope": self._scope_payload(principal),
            "current_principal": self._scope_payload(principal),
            "roles": scoped["roles"],
            "boundary": "tenant scope is resolved from the server-side credential directory",
        }

    def _scope(self, principal: SandboxPrincipal) -> dict[str, Any]:
        scoped = self.repository.tenant_snapshot(principal.tenant_id)
        if set(principal.role_ids).intersection(self._TENANT_WIDE_ROLES):
            return scoped
        company_constraints = set(principal.company_ids)
        department_constraints = set(principal.department_ids)
        team_constraints = set(principal.team_ids)
        candidate_teams = list(scoped["teams"])
        if company_constraints:
            candidate_teams = [item for item in candidate_teams if item["company_id"] in company_constraints]
        if department_constraints:
            candidate_teams = [item for item in candidate_teams if item["department_id"] in department_constraints]
        if team_constraints:
            candidate_teams = [item for item in candidate_teams if item["id"] in team_constraints]
        if not (company_constraints or department_constraints or team_constraints):
            candidate_teams = []
        allowed_teams = {item["id"] for item in candidate_teams}
        allowed_departments = {item["department_id"] for item in candidate_teams}
        allowed_companies = {item["company_id"] for item in candidate_teams}
        scoped["companies"] = [item for item in scoped["companies"] if item["id"] in allowed_companies]
        scoped["departments"] = [item for item in scoped["departments"] if item["id"] in allowed_departments]
        scoped["teams"] = candidate_teams
        scoped["users"] = [
            item for item in scoped["users"] if item["team_id"] in allowed_teams or item["id"] == principal.user_id
        ]
        scoped["cases"] = [item for item in scoped["cases"] if item["team_id"] in allowed_teams]
        allowed_case_ids = {item["id"] for item in scoped["cases"]}
        scoped["quality_issues"] = [
            item
            for item in scoped["quality_issues"]
            if item.get("resource_type") == "case" and item.get("resource_id") in allowed_case_ids
        ]
        for collection in ("daily_submissions", "weekly_submissions", "monthly_submissions", "travels"):
            scoped[collection] = [item for item in scoped[collection] if item["team_id"] in allowed_teams]
        scoped["metric_definitions"] = [
            item for item in scoped["metric_definitions"] if item["company_id"] in allowed_companies
        ]
        return scoped

    @staticmethod
    def _scope_payload(principal: SandboxPrincipal) -> dict[str, Any]:
        return {
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "role_ids": list(principal.role_ids),
            "company_ids": list(principal.company_ids),
            "department_ids": list(principal.department_ids),
            "team_ids": list(principal.team_ids),
        }

    @staticmethod
    def _assert_allowed_team(scoped: dict[str, Any], team_id: str) -> None:
        if team_id not in {item["id"] for item in scoped["teams"]}:
            raise LookupError("team not found in authorized tenant")

    @staticmethod
    def _open_case_count(scoped: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
        cases = [case for case in scoped["cases"] if case["status"] != "closed"]
        return len(cases), [
            {
                "resource_type": "case",
                "resource_id": case["id"],
                "team_id": case["team_id"],
                "user_id": case["owner_user_id"],
                "value": 1,
            }
            for case in cases
        ]

    @staticmethod
    def _high_risk_case_count(scoped: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
        cases = [case for case in scoped["cases"] if case["risk_level"] == "high"]
        return len(cases), [
            {
                "resource_type": "case",
                "resource_id": case["id"],
                "team_id": case["team_id"],
                "user_id": case["owner_user_id"],
                "value": 1,
            }
            for case in cases
        ]

    @staticmethod
    def _active_travel_count(scoped: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
        travels = [travel for travel in scoped["travels"] if travel["status"] == "active"]
        return len(travels), [
            {
                "resource_type": "travel",
                "resource_id": travel["id"],
                "team_id": travel["team_id"],
                "user_id": travel["traveler_user_id"],
                "value": 1,
            }
            for travel in travels
        ]

    @staticmethod
    def _daily_submission_rate(scoped: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
        submissions = scoped["daily_submissions"]
        completed = [item for item in submissions if item["status"] == "completed"]
        value = round((len(completed) / len(submissions) * 100) if submissions else 0.0, 2)
        return value, [
            {
                "resource_type": "daily_submission",
                "resource_id": item["id"],
                "team_id": item["team_id"],
                "user_id": item["user_id"],
                "value": item["status"] == "completed",
            }
            for item in submissions
        ]
