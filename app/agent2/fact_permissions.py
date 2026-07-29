from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _csv_env(name: str) -> frozenset[str]:
    return frozenset(
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    )


TEAM_LEADER_ROLES = {"team_lead", "team_leader", "team_manager", "leader", "manager", "负责人", "团队负责人"}
DEPARTMENT_HEAD_ROLES = {"department_head", "dept_head", "department_manager", "admin", "部门负责人", "部长"}
ALL_ACCESS_DINGTALK_USER_IDS = _csv_env(
    "AGENT2_FACT_ALL_ACCESS_DINGTALK_USER_IDS"
)
ALL_ACCESS_NAMES = _csv_env("AGENT2_FACT_ALL_ACCESS_NAMES")


@dataclass(frozen=True)
class FactPermissionDecision:
    allowed: bool
    policy: str
    reason: str
    requester_scope: dict[str, str] = field(default_factory=dict)
    target_scope: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": True,
            "allowed": self.allowed,
            "policy": self.policy,
            "reason": self.reason,
            "requester_scope": dict(self.requester_scope),
            "target_scope": dict(self.target_scope),
        }


def evaluate_case_fact_permission(*, query: Any, facts: dict[str, Any]) -> FactPermissionDecision:
    requester = _requester_scope(query)
    target = _target_scope(facts)
    policy = "case_fact_scope_v1"

    if not _has_explicit_requester_metadata(query):
        return FactPermissionDecision(True, policy, "requester context absent; compatibility allow", requester, target)

    if _has_all_access(requester):
        return FactPermissionDecision(True, policy, "requester has all-scope access", requester, target)

    role = _normalized_role(requester.get("role", ""))
    if role in TEAM_LEADER_ROLES or role in DEPARTMENT_HEAD_ROLES:
        return FactPermissionDecision(True, policy, "requester role can access cross-team facts", requester, target)

    if _is_self_scope(requester, target):
        return FactPermissionDecision(True, policy, "requester can access self facts", requester, target)

    if _is_own_team_scope(requester, target):
        return FactPermissionDecision(True, policy, "requester can access own-team facts", requester, target)

    return FactPermissionDecision(False, policy, "requester cannot access cross-team or all-scope facts", requester, target)


def _requester_scope(query: Any) -> dict[str, str]:
    metadata = getattr(query, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    requester = metadata.get("requester")
    requester = requester if isinstance(requester, dict) else {}
    return {
        "user_id": str(requester.get("user_id") or getattr(query, "user_id", "") or ""),
        "dingtalk_user_id": str(requester.get("dingtalk_user_id") or getattr(query, "dingtalk_user_id", "") or ""),
        "name": str(requester.get("name") or ""),
        "role": str(requester.get("role") or "member"),
        "team_id": str(requester.get("team_id") or ""),
        "team_name": str(requester.get("team_name") or ""),
        "department_name": str(requester.get("department_name") or ""),
    }


def _has_explicit_requester_metadata(query: Any) -> bool:
    metadata = getattr(query, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return isinstance(metadata.get("requester"), dict)


def _target_scope(facts: dict[str, Any]) -> dict[str, str]:
    group_by = str(facts.get("group_by") or "")
    department = str(facts.get("department") or "")
    assignee_name = _clean_person_name(str(facts.get("assignee_name") or ""))
    scope_type = "all"
    if group_by == "department":
        scope_type = "all_teams"
    elif department:
        scope_type = "team"
    elif assignee_name:
        scope_type = "person"
    return {
        "scope_type": scope_type,
        "department": department,
        "assignee_name": assignee_name,
        "group_by": group_by,
    }


def _has_all_access(requester: dict[str, str]) -> bool:
    return (
        requester.get("dingtalk_user_id") in ALL_ACCESS_DINGTALK_USER_IDS
        or requester.get("name") in ALL_ACCESS_NAMES
    )


def _is_self_scope(requester: dict[str, str], target: dict[str, str]) -> bool:
    assignee = _clean_person_name(target.get("assignee_name", ""))
    name = _clean_person_name(requester.get("name", ""))
    return bool(assignee and name and assignee == name)


def _is_own_team_scope(requester: dict[str, str], target: dict[str, str]) -> bool:
    department = _normalize_scope_name(target.get("department", ""))
    if not department:
        return False
    team_name = _normalize_scope_name(requester.get("team_name", ""))
    department_name = _normalize_scope_name(requester.get("department_name", ""))
    return department in {team_name, department_name}


def _clean_person_name(value: str) -> str:
    text = str(value or "").strip()
    if ")" in text:
        text = text.rsplit(")", 1)[-1]
    if "）" in text:
        text = text.rsplit("）", 1)[-1]
    return text.strip()


def _normalize_scope_name(value: str) -> str:
    return str(value or "").strip().replace(" ", "")


def _normalized_role(role: str) -> str:
    return str(role or "").strip().lower()
