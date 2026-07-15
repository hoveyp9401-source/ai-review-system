from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import Agent2Case, Agent2IdentityBinding, PeriodicReport, TravelIntent
from app.models import DailyReport

from app.legal_ops.business_labels import (
    case_stage_label,
    case_type_label,
    report_status_label,
    source_label,
    travel_status_label,
)


def project_case_workspace(
    read_model: dict[str, Any],
    *,
    page: int = 1,
    page_size: int = 20,
    case_type: str = "",
    stage: str = "",
    query: str = "",
    progress_status: str = "",
    principal_user_id: str = "",
    writable_case_ids: tuple[str, ...] | None = None,
    permission_mode: str = "",
) -> dict[str, Any]:
    safe_page = max(1, int(page))
    safe_page_size = max(1, min(100, int(page_size)))
    identities = {
        str(item.get("user_id") or ""): str(item.get("display_name") or "")
        for item in read_model.get("identity_bindings", [])
    }
    party_names = {
        str(item.get("party_id") or ""): str(item.get("canonical_name") or "")
        for item in read_model.get("parties", [])
    }
    counterparties: dict[str, list[str]] = defaultdict(list)
    for role in read_model.get("party_case_roles", []):
        name = party_names.get(str(role.get("party_id") or ""), "")
        if name and name not in counterparties[str(role.get("case_id") or "")]:
            counterparties[str(role.get("case_id") or "")].append(name)
    progress_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for progress in read_model.get("case_progress", []):
        if not progress.get("deleted_at"):
            progress_by_case[str(progress.get("case_id") or "")].append(progress)
    lifecycle_by_case = {
        str(item.get("case_id") or ""): item
        for item in read_model.get("case_lifecycle_states", [])
    }
    policy_by_case = {
        str(item.get("case_id") or ""): item
        for item in read_model.get("case_followup_policies", [])
    }
    cadence_labels = {
        "daily": "每天一次", "weekly": "每周一次", "every_15_days": "每 15 天一次",
        "monthly": "每月一次", "custom_interval": "自定义周期", "event_only": "仅关键节点",
        "manual_only": "仅人工追问", "paused": "已暂停", "disabled": "已关闭",
    }
    needle = str(query or "").strip().casefold()
    writable_scope = (
        {str(value) for value in writable_case_ids}
        if writable_case_ids is not None
        else None
    )
    filtered: list[dict[str, Any]] = []
    all_cases = [item for item in read_model.get("cases", []) if isinstance(item, dict)]
    for item in all_cases:
        case_id = str(item.get("case_id") or "")
        raw_type = str(item.get("case_type") or "")
        raw_stage = str(item.get("status") or "")
        if case_type and raw_type != case_type:
            continue
        if stage and raw_stage != stage:
            continue
        if progress_status == "missing" and progress_by_case.get(case_id):
            continue
        if progress_status == "active" and not progress_by_case.get(case_id):
            continue
        searchable = " ".join(
            (
                str(item.get("case_name") or ""),
                str(item.get("case_number") or ""),
                str(item.get("external_case_id") or ""),
                " ".join(counterparties.get(str(item.get("case_id") or ""), [])),
            )
        ).casefold()
        if needle and needle not in searchable:
            continue
        progress = sorted(
            progress_by_case.get(case_id, []),
            key=lambda row: str(row.get("updated_at") or row.get("occurred_at") or ""),
            reverse=True,
        )
        lifecycle = lifecycle_by_case.get(case_id, {})
        policy = policy_by_case.get(case_id, {})
        filtered.append(
            {
                "case_ref": case_id,
                "case_name": str(item.get("case_name") or "未命名案件"),
                "case_number": str(
                    item.get("case_number") or item.get("external_case_id") or "暂未记录"
                ),
                "case_type": case_type_label(raw_type),
                "case_type_code": raw_type,
                "stage": case_stage_label(raw_type, raw_stage),
                "stage_code": raw_stage,
                "node": str(lifecycle.get("node") or "暂未记录"),
                "owner_name": identities.get(str(item.get("owner_user_id") or ""), "未识别负责人"),
                "assignment": (
                    "本人负责"
                    if principal_user_id
                    and str(item.get("owner_user_id") or "") == principal_user_id
                    else "团队协作"
                    if principal_user_id
                    else "授权案件"
                ),
                "can_add_progress": bool(
                    principal_user_id
                    and (
                        writable_scope is None
                        or case_id in writable_scope
                    )
                ),
                "counterparties": counterparties.get(case_id, []),
                "latest_progress": (
                    str(progress[0].get("summary") or "") if progress else "暂无有效进展"
                ),
                "progress_count": len(progress),
                "next_plan": "；".join(
                    str(value) for value in lifecycle.get("next_actions_json") or []
                ) or "暂未记录",
                "hearing_date": "暂未记录",
                "risk_level": "暂未评估",
                "court": "暂未记录",
                "cause": "暂未记录",
                "followup_policy": cadence_labels.get(
                    str(policy.get("cadence_type") or ""), "暂未配置"
                ),
                "source": source_label(str(item.get("source_type") or "")),
                "updated_at": str(item.get("updated_at") or ""),
            }
        )
    filtered.sort(key=lambda row: row["updated_at"], reverse=True)
    total = len(filtered)
    start = (safe_page - 1) * safe_page_size
    stage_counts = Counter(
        case_stage_label(str(item.get("case_type") or ""), str(item.get("status") or ""))
        for item in all_cases
    )
    assigned_to_me = sum(
        bool(principal_user_id)
        and str(item.get("owner_user_id") or "") == principal_user_id
        for item in all_cases
    )
    return {
        "summary": {
            "total": len(all_cases),
            "plaintiff": sum(item.get("case_type") == "plaintiff_case" for item in all_cases),
            "defendant": sum(item.get("case_type") == "defendant_case" for item in all_cases),
            "with_progress": len(progress_by_case),
            "without_progress": max(0, len(all_cases) - len(progress_by_case)),
            "stage_distribution": dict(stage_counts),
            "assigned_to_me": assigned_to_me,
            "shared_with_me": (
                max(0, len(all_cases) - assigned_to_me) if principal_user_id else 0
            ),
            "writable": (
                len(writable_scope.intersection({str(item.get("case_id") or "") for item in all_cases}))
                if writable_scope is not None
                else len(all_cases) if principal_user_id else 0
            ),
            "access_label": (
                "团队共享协作"
                if permission_mode == "explicit_shared_scope"
                else "授权案件"
            ),
        },
        "filters": {
            "case_type": case_type,
            "stage": stage,
            "query": query,
            "progress_status": progress_status,
        },
        "pagination": {
            "page": safe_page,
            "page_size": safe_page_size,
            "total": total,
            "pages": max(1, math.ceil(total / safe_page_size)),
        },
        "items": filtered[start : start + safe_page_size],
    }


def project_case_detail(
    detail: dict[str, Any],
    followup: dict[str, Any],
    *,
    identity_names: dict[str, str],
    can_manage_followup: bool,
    editable_actor_user_id: str = "",
    writable_case_ids: tuple[str, ...] | None = None,
    permission_mode: str = "",
) -> dict[str, Any]:
    case = detail.get("case") or {}
    case_type = str(case.get("case_type") or "")
    current_stage = str(case.get("status") or "")
    stage_codes = (
        ["intended_filing", "litigation", "enforcement", "closed"]
        if case_type == "plaintiff_case"
        else ["accepted", "hearing", "adjudicated", "performance", "closed"]
        if case_type == "defendant_case"
        else []
    )
    current_index = stage_codes.index(current_stage) if current_stage in stage_codes else -1
    lifecycle = [
        {
            "label": case_stage_label(case_type, code),
            "state": (
                "current" if index == current_index
                else "completed" if current_index >= 0 and index < current_index
                else "future"
            ),
        }
        for index, code in enumerate(stage_codes)
    ]
    role_labels = {
        "plaintiff": "原告", "defendant": "被告", "applicant": "申请人",
        "respondent": "被申请人", "judgment_creditor": "申请执行人",
        "judgment_debtor": "被执行人", "third_party": "第三人",
    }
    parties = [
        {
            "name": str((row.get("party") or {}).get("canonical_name") or "未命名主体"),
            "role": role_labels.get(
                str((row.get("role") or {}).get("role_type") or ""), "其他当事人"
            ),
        }
        for row in detail.get("parties", [])
    ]
    case_id = str(case.get("case_id") or "")
    can_add_progress = bool(
        editable_actor_user_id
        and (
            writable_case_ids is None
            or case_id in {str(value) for value in writable_case_ids}
        )
    )
    progress = [
        {
            "content": str(node.get("title") or ""),
            "occurred_at": str(node.get("occurred_at") or ""),
            "origin": source_label(str(node.get("content_origin") or "")),
            "deleted": bool(node.get("deleted")),
            "actions": {
                "progress_ref": str(node.get("progress_id") or ""),
                "expected_version": int(node.get("version") or 0),
                "can_edit": bool(
                    node.get("progress_id")
                    and not node.get("deleted")
                    and editable_actor_user_id
                    and str(node.get("reporter_id") or "")
                    == editable_actor_user_id
                ),
            },
        }
        for node in (detail.get("lifecycle") or {}).get("internal_progress_nodes", [])
        if not node.get("deleted")
    ]
    audit_action_labels = {
        "CreateCaseProgress": "新增案件进展",
        "UpdateCaseProgress": "修改案件进展",
        "DeleteCaseProgress": "删除案件进展",
        "LinkCaseProgress": "关联案件进展",
        "create_case_progress": "新增案件进展",
        "update_case_progress": "修改案件进展",
        "delete_case_progress": "删除案件进展",
        "link_case_progress": "关联案件进展",
    }
    audit_source_labels = {
        "legal_ops_web": "法务业务中台",
        "legal_ops_ui": "法务业务中台",
        "dingtalk_stream": "钉钉对话",
        "dingtalk_text": "钉钉对话",
        "case_lifecycle_followup": "案件主动追问",
    }
    audit = [
        {
            "actor_name": identity_names.get(
                str(item.get("actor_user_id") or ""), "未识别操作人"
            ),
            "action": audit_action_labels.get(
                str(item.get("command_type") or ""), "其他案件操作"
            ),
            "result": "已写入",
            "source": audit_source_labels.get(
                str(item.get("source_channel") or ""), "其他业务入口"
            ),
            "created_at": str(item.get("created_at") or ""),
        }
        for item in reversed(detail.get("audits", []))
        if isinstance(item, dict)
    ]
    report_type_labels = {"daily": "日报", "weekly": "周报", "monthly": "月报"}
    projection_section_labels = {
        "today_work": "今日工作",
        "future_plan": "下一步计划",
        "tomorrow_plan": "下一步计划",
    }
    projection_status_labels = {
        "active": "已关联",
        "removed": "已撤销",
        "failed": "关联失败",
    }
    report_projection = [
        {
            "report_type": report_type_labels.get(
                str(item.get("report_type") or ""), "报告"
            ),
            "section": projection_section_labels.get(
                str(item.get("projection_type") or ""), "其他区块"
            ),
            "status": projection_status_labels.get(
                str(item.get("status") or ""), "状态异常"
            ),
            "created_at": str(item.get("created_at") or ""),
        }
        for item in detail.get("report_projections", [])
        if isinstance(item, dict)
    ]
    lifecycle_state = followup.get("lifecycle_state") or {}
    policy = followup.get("policy") or {}
    latest_status = followup.get("latest_status") or {}
    cadence_labels = {
        "daily": "每天一次", "weekly": "每周一次", "every_15_days": "每 15 天一次",
        "monthly": "每月一次", "custom_interval": "自定义周期", "event_only": "仅关键节点",
        "manual_only": "仅人工追问", "paused": "已暂停", "disabled": "已关闭",
    }
    payload: dict[str, Any] = {
        "case_ref": case_id,
        "case_name": str(case.get("case_name") or "未命名案件"),
        "case_number": str(case.get("case_number") or case.get("external_case_id") or "暂未记录"),
        "case_type": case_type_label(case_type),
        "stage": case_stage_label(case_type, current_stage),
        "owner_name": identity_names.get(str(case.get("owner_user_id") or ""), "未识别负责人"),
        "assignment": (
            "本人负责"
            if editable_actor_user_id
            and str(case.get("owner_user_id") or "") == editable_actor_user_id
            else "团队协作"
            if editable_actor_user_id and permission_mode == "explicit_shared_scope"
            else "授权案件"
        ),
        "can_add_progress": can_add_progress,
        "source": source_label(str(case.get("source_type") or "")),
        "lifecycle": lifecycle,
        "current_node": str(lifecycle_state.get("node") or "暂未记录"),
        "current_status": str(lifecycle_state.get("current_status") or "暂未记录"),
        "next_plan": [str(item) for item in lifecycle_state.get("next_actions_json") or []],
        "hearing_readiness": str(lifecycle_state.get("hearing_readiness") or "暂未记录"),
        "risk_level": "暂未评估",
        "parties": parties,
        "progress": progress,
        "audit": audit,
        "report_projection": report_projection,
        "clues": [
            {
                "type": str(item.get("clue_type") or "其他"),
                "summary": str(item.get("summary") or ""),
            }
            for item in detail.get("business_clues", [])
        ],
        "followup": {
            "cadence": cadence_labels.get(str(policy.get("cadence_type") or ""), "暂未配置"),
            "enabled": bool(policy.get("enabled")) if policy else False,
            "waiting_for_reply": bool(latest_status.get("waiting_for_reply")),
            "last_followup_at": str(latest_status.get("last_followup_at") or ""),
            "next_due_at": str(latest_status.get("next_due_at") or ""),
            "last_message_status": travel_status_label(
                str(latest_status.get("last_message_status") or "")
            ) if latest_status.get("last_message_status") else "暂无通知",
        },
        "can_manage_followup": can_manage_followup,
    }
    if can_manage_followup:
        payload["management"] = {
            "case_ref": str(case.get("case_id") or ""),
            "owner_ref": str(case.get("owner_user_id") or ""),
            "expected_version": int(policy.get("version") or 0),
            "cadence_type": str(policy.get("cadence_type") or "event_only"),
            "enabled": bool(policy.get("enabled")),
            "hearing_reminders_enabled": bool(policy.get("hearing_reminders_enabled", True)),
            "stage_transition_enabled": bool(policy.get("stage_transition_enabled", True)),
            "node_transition_enabled": bool(policy.get("node_transition_enabled", True)),
        }
    return payload


def project_report_center(
    daily_reports: list[Any],
    periodic_reports: list[Any],
    *,
    identity_names: dict[str, str],
    editable_owner_user_id: str = "",
    current_date: str = "",
) -> dict[str, Any]:
    today = date.fromisoformat(current_date) if current_date else date.today()
    items: list[dict[str, Any]] = []
    for row in daily_reports:
        user_id = str(_value(row, "user_id") or "")
        section_values = {
            "today_work": list(_value(row, "today_work") or []),
            "problems": list(_value(row, "problems") or []),
            "tomorrow_plan": list(_value(row, "tomorrow_plan") or []),
        }
        section_status = _value(row, "section_status") or {}
        section_status = section_status if isinstance(section_status, dict) else {}
        raw_item_ids = section_status.get("_draft_item_ids") or {}
        raw_item_ids = raw_item_ids if isinstance(raw_item_ids, dict) else {}
        action_items: dict[str, list[dict[str, str]]] = {}
        for field_name, values in section_values.items():
            refs = raw_item_ids.get(field_name) or []
            refs = refs if isinstance(refs, list) else []
            action_items[field_name] = [
                {
                    "item_ref": str(refs[index]) if index < len(refs) else "",
                    "value": str(value),
                }
                for index, value in enumerate(values)
            ]
        items.append(
            {
                "report_ref": str(_value(row, "id") or ""),
                "report_type": "日报",
                "report_type_code": "daily",
                "period": str(_value(row, "report_date") or ""),
                "owner_name": identity_names.get(user_id, "未识别人员"),
                "status": report_status_label(str(_value(row, "status") or "")),
                "status_code": str(_value(row, "status") or ""),
                "sections": {
                    "今日工作": section_values["today_work"],
                    "问题与风险": section_values["problems"],
                    "下一步计划": section_values["tomorrow_plan"],
                },
                "source": _report_source_label(str(_value(row, "source") or "")),
                "updated_at": str(_value(row, "updated_at") or ""),
                "actions": {
                    "can_edit": bool(
                        editable_owner_user_id
                        and user_id == editable_owner_user_id
                        and str(_value(row, "status") or "") == "collecting"
                    ),
                    "expected_version": int(section_status.get("_agent2_report_version") or 0),
                    "items": action_items,
                },
            }
        )
    section_labels = {
        "completed": "本期完成",
        "accomplishments": "本期完成",
        "key_work": "重点工作",
        "problems": "问题与风险",
        "risks": "问题与风险",
        "next_plan": "下一步计划",
        "plan": "下一步计划",
        "summary": "总结",
        "metrics": "关键指标",
    }
    for row in periodic_reports:
        report_type = str(_value(row, "report_type") or "")
        sections = _value(row, "sections_json") or {}
        visible_sections: dict[str, list[str]] = {}
        if isinstance(sections, dict):
            for key, value in sections.items():
                label = section_labels.get(str(key), str(key))
                values = value if isinstance(value, list) else [value] if value else []
                visible_sections[label] = [str(item) for item in values]
        raw_item_ids = _value(row, "item_ids_json") or {}
        raw_item_ids = raw_item_ids if isinstance(raw_item_ids, dict) else {}
        action_items = {
            str(field_name): [
                {
                    "item_ref": str(refs[index]) if index < len(refs) else "",
                    "value": str(value),
                }
                for index, value in enumerate(
                    (sections.get(field_name) if isinstance(sections.get(field_name), list) else [])
                )
            ]
            for field_name, refs in raw_item_ids.items()
            if isinstance(refs, list)
        }
        owner_user_id = str(_value(row, "owner_user_id") or "")
        current_period_key = (
            f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
            if report_type == "weekly"
            else f"{today.year:04d}-{today.month:02d}"
        )
        items.append(
            {
                "report_ref": str(_value(row, "report_id") or ""),
                "report_type": "周报" if report_type == "weekly" else "月报",
                "report_type_code": report_type,
                "period": str(_value(row, "period_key") or ""),
                "owner_name": identity_names.get(owner_user_id, "未识别人员"),
                "status": report_status_label(str(_value(row, "status") or "")),
                "status_code": str(_value(row, "status") or ""),
                "sections": visible_sections,
                "source": _report_source_label(str(_value(row, "source_channel") or "")),
                "updated_at": str(_value(row, "updated_at") or ""),
                "actions": {
                    "can_edit": bool(
                        editable_owner_user_id
                        and owner_user_id == editable_owner_user_id
                        and str(_value(row, "status") or "") == "collecting"
                        and str(_value(row, "period_key") or "") == current_period_key
                    ),
                    "expected_version": int(_value(row, "version") or 0),
                    "items": action_items,
                },
            }
        )
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    counts = Counter(item["report_type_code"] for item in items)
    return {
        "summary": {
            "total": len(items),
            "daily": counts["daily"],
            "weekly": counts["weekly"],
            "monthly": counts["monthly"],
            "collecting": sum(item["status_code"] == "collecting" for item in items),
        },
        "items": items,
    }


def project_team_center(
    bindings: list[Any],
    cases: list[Any],
    daily_reports: list[Any],
    periodic_reports: list[Any],
    travel_intents: list[Any],
) -> dict[str, Any]:
    member_rows: list[dict[str, Any]] = []
    for binding in bindings:
        user_id = str(_value(binding, "user_id") or "")
        owned_cases = [row for row in cases if str(_value(row, "owner_user_id") or "") == user_id]
        user_daily = [row for row in daily_reports if str(_value(row, "user_id") or "") == user_id]
        user_daily.sort(key=lambda row: str(_value(row, "report_date") or ""), reverse=True)
        user_periodic = [
            row for row in periodic_reports
            if str(_value(row, "owner_user_id") or "") == user_id
        ]
        user_travel = [
            row for row in travel_intents
            if str(_value(row, "user_id") or "") == user_id
            and str(_value(row, "status") or "") not in {"cancelled", "completed"}
        ]
        member_rows.append(
            {
                "name": str(_value(binding, "display_name") or "未命名成员"),
                "assigned_cases": len(owned_cases),
                "plaintiff_cases": sum(
                    _value(row, "case_type") == "plaintiff_case" for row in owned_cases
                ),
                "defendant_cases": sum(
                    _value(row, "case_type") == "defendant_case" for row in owned_cases
                ),
                "latest_daily_date": (
                    str(_value(user_daily[0], "report_date") or "") if user_daily else "暂无"
                ),
                "latest_daily_status": (
                    report_status_label(str(_value(user_daily[0], "status") or ""))
                    if user_daily else "暂无日报"
                ),
                "periodic_reports": len(user_periodic),
                "active_travel": len(user_travel),
            }
        )
    return {
        "team_name": "Agent2 灰测法务团队",
        "summary": {
            "members": len(member_rows),
            "cases": len(cases),
            "active_travel": sum(
                str(_value(row, "status") or "") not in {"cancelled", "completed"}
                for row in travel_intents
            ),
        },
        "members": member_rows,
    }


def project_travel_center(read_model: dict[str, Any]) -> dict[str, Any]:
    identity_names = {
        str(item.get("user_id") or ""): str(item.get("display_name") or "未识别人员")
        for item in read_model.get("identity_bindings", [])
    }
    business_travel_intents = [
        item
        for item in read_model.get("travel_intents", [])
        if _is_default_business_record(item)
    ]
    travels = [
        {
            "travel_ref": str(item.get("travel_intent_id") or ""),
            "traveler_name": identity_names.get(str(item.get("user_id") or ""), "未识别人员"),
            "destination": str(item.get("destination_normalized") or item.get("destination_raw") or "暂未记录"),
            "start_at": str(item.get("start_at") or ""),
            "end_at": str(item.get("end_at") or ""),
            "purpose": str(item.get("purpose_summary") or "未填写事由"),
            "status": travel_status_label(str(item.get("status") or "")),
            "source": _travel_source_label(item),
        }
        for item in business_travel_intents
    ]
    candidates: list[dict[str, Any]] = []
    for item in read_model.get("collaboration_candidates", []):
        if not _is_default_business_record(item):
            continue
        responses = item.get("responses_json") if isinstance(item.get("responses_json"), dict) else {}
        candidate_responses = []
        for user_id in item.get("participant_ids") or []:
            value = responses.get(user_id, "waiting_for_reply")
            raw_status = str(value.get("status") if isinstance(value, dict) else value or "waiting_for_reply")
            candidate_responses.append(
                {
                    "participant": identity_names.get(str(user_id), "未识别人员"),
                    "status": travel_status_label(raw_status),
                }
            )
        candidates.append(
            {
                "candidate_ref": str(item.get("candidate_id") or ""),
                "destination": str(item.get("destination") or "暂未记录"),
                "overlap_start": str(item.get("overlap_start") or ""),
                "overlap_end": str(item.get("overlap_end") or ""),
                "participants": [
                    identity_names.get(str(user_id), "未识别人员")
                    for user_id in item.get("participant_ids") or []
                ],
                "responses": candidate_responses,
                "status": travel_status_label(str(item.get("status") or "")),
            }
        )
    travel_notifications = [
        item
        for item in read_model.get("notifications", [])
        if _is_default_business_record(item)
        and (
            str(item.get("message_type") or "").startswith("travel_")
            or bool(item.get("candidate_id"))
        )
    ]
    notifications = [
        {
            "recipient_name": identity_names.get(
                str(item.get("recipient_user_id") or ""), "未识别人员"
            ),
            "status": travel_status_label(str(item.get("status") or "")),
            "delivery_claim": (
                "已确认送达"
                if str(item.get("status") or "") == "delivery_confirmed"
                else "未确认送达"
            ),
            "sent_at": str(item.get("sent_at") or ""),
            "retry_count": int(item.get("retry_count") or 0),
            "error": str(item.get("error_message") or ""),
        }
        for item in travel_notifications
    ]
    return {
        "summary": {
            "travels": len(travels),
            "candidates": len(candidates),
            "notifications": len(notifications),
            "waiting_for_reply": sum(
                item["status"] in {"待确认协同", "已创建通知", "一方已接受"}
                for item in candidates
            ),
        },
        "travels": travels,
        "candidates": candidates,
        "notifications": notifications,
    }


async def load_travel_center(
    session: AsyncSession,
    *,
    tenant_id: str,
    read_model: dict[str, Any],
) -> dict[str, Any]:
    participant_ids = {
        str(user_id)
        for candidate in read_model.get("collaboration_candidates", [])
        for user_id in (candidate.get("participant_ids") or [])
        if user_id
    }
    participant_ids.update(
        str(item.get("user_id"))
        for item in read_model.get("travel_intents", [])
        if item.get("user_id")
    )
    identities = []
    if participant_ids:
        identities = list(
            (
                await session.scalars(
                    select(Agent2IdentityBinding).where(
                        Agent2IdentityBinding.tenant_id == tenant_id,
                        Agent2IdentityBinding.active.is_(True),
                        Agent2IdentityBinding.user_id.in_(tuple(participant_ids)),
                    )
                )
            ).all()
        )
    enriched = dict(read_model)
    enriched["identity_bindings"] = [
        {"user_id": str(item.user_id), "display_name": item.display_name}
        for item in identities
    ]
    return project_travel_center(enriched)


def _travel_source_label(item: dict[str, Any]) -> str:
    origin = str(item.get("data_origin") or "")
    if origin:
        return source_label(origin)
    channel = str(item.get("source_channel") or "").lower()
    if "dingtalk" in channel:
        return "真实用户消息"
    if "smoke" in channel:
        return "服务器验收记录"
    return "系统记录"


def _is_default_business_record(item: dict[str, Any]) -> bool:
    excluded_origins = {
        "server_acceptance_smoke",
        "sandbox_fixture",
        "seed_fixture",
        "frontend_static",
        "mock_transport",
    }
    origin = str(item.get("data_origin") or "").strip().lower()
    channel = str(item.get("source_channel") or "").strip().lower()
    return origin not in excluded_origins and not any(
        marker in channel for marker in ("acceptance_smoke", "fixture", "mock_transport")
    )


async def load_report_center(
    session: AsyncSession,
    *,
    tenant_id: str,
    principal_user_id: str = "",
) -> dict[str, Any]:
    identity_statement = select(Agent2IdentityBinding).where(
        Agent2IdentityBinding.tenant_id == tenant_id,
        Agent2IdentityBinding.active.is_(True),
    )
    if principal_user_id:
        identity_statement = identity_statement.where(
            Agent2IdentityBinding.user_id == principal_user_id
        )
    identities = list((await session.scalars(identity_statement)).all())
    identity_names = {str(item.user_id): item.display_name for item in identities}
    user_ids = tuple(identity_names)
    user_uuids = tuple(
        parsed for value in user_ids if (parsed := _try_uuid(value)) is not None
    )
    daily_reports = []
    if user_uuids:
        daily_reports = list(
            (
                await session.scalars(
                    select(DailyReport)
                    .where(DailyReport.user_id.in_(user_uuids))
                    .order_by(DailyReport.report_date.desc(), DailyReport.updated_at.desc())
                )
            ).all()
        )
    periodic_reports = list(
        (
            await session.scalars(
                select(PeriodicReport)
                .where(
                    PeriodicReport.tenant_id == tenant_id,
                    PeriodicReport.owner_user_id.in_(user_ids),
                )
                .order_by(PeriodicReport.period_start.desc(), PeriodicReport.updated_at.desc())
            )
        ).all()
    ) if user_ids else []
    return project_report_center(
        daily_reports,
        periodic_reports,
        identity_names=identity_names,
        editable_owner_user_id=principal_user_id,
        current_date=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    )


async def load_team_center(
    session: AsyncSession,
    *,
    tenant_id: str,
    team_id: str = "",
) -> dict[str, Any]:
    identity_statement = select(Agent2IdentityBinding).where(
        Agent2IdentityBinding.tenant_id == tenant_id,
        Agent2IdentityBinding.active.is_(True),
    )
    if team_id:
        identity_statement = identity_statement.where(Agent2IdentityBinding.team_id == team_id)
    bindings = list((await session.scalars(identity_statement)).all())
    user_ids = tuple(str(item.user_id) for item in bindings)
    user_uuids = tuple(
        parsed for value in user_ids if (parsed := _try_uuid(value)) is not None
    )
    cases = list(
        (
            await session.scalars(
                select(Agent2Case).where(
                    Agent2Case.tenant_id == tenant_id,
                    Agent2Case.owner_user_id.in_(user_ids),
                )
            )
        ).all()
    ) if user_ids else []
    cases = [
        item for item in cases
        if not (isinstance(item.source_json, dict) and item.source_json.get("display_hidden") is True)
    ]
    daily_reports = list(
        (
            await session.scalars(
                select(DailyReport)
                .where(DailyReport.user_id.in_(user_uuids))
                .order_by(DailyReport.report_date.desc())
            )
        ).all()
    ) if user_uuids else []
    periodic_reports = list(
        (
            await session.scalars(
                select(PeriodicReport).where(
                    PeriodicReport.tenant_id == tenant_id,
                    PeriodicReport.owner_user_id.in_(user_ids),
                )
            )
        ).all()
    ) if user_ids else []
    travel_intents = list(
        (
            await session.scalars(
                select(TravelIntent).where(
                    TravelIntent.tenant_id == tenant_id,
                    TravelIntent.user_id.in_(user_ids),
                )
            )
        ).all()
    ) if user_ids else []
    return project_team_center(
        bindings, cases, daily_reports, periodic_reports, travel_intents
    )


async def load_case_detail_workspace(
    session: AsyncSession,
    *,
    tenant_id: str,
    case_id: str,
    allowed_case_ids: tuple[str, ...] | None,
    can_manage_followup: bool,
    editable_actor_user_id: str = "",
    writable_case_ids: tuple[str, ...] | None = None,
    permission_mode: str = "",
) -> dict[str, Any]:
    from app.legal_ops.phase2_read import (
        load_case_followup_configuration,
        load_phase2_case_detail,
    )

    detail = await load_phase2_case_detail(
        session,
        tenant_id=tenant_id,
        case_id=case_id,
        allowed_case_ids=allowed_case_ids,
    )
    followup = await load_case_followup_configuration(
        session,
        tenant_id=tenant_id,
        case_id=case_id,
        allowed_case_ids=allowed_case_ids,
    )
    owner_user_id = str((detail.get("case") or {}).get("owner_user_id") or "")
    identity = await session.scalar(
        select(Agent2IdentityBinding).where(
            Agent2IdentityBinding.tenant_id == tenant_id,
            Agent2IdentityBinding.user_id == owner_user_id,
            Agent2IdentityBinding.active.is_(True),
        )
    )
    return project_case_detail(
        detail,
        followup,
        identity_names=(
            {owner_user_id: identity.display_name} if identity is not None else {}
        ),
        can_manage_followup=can_manage_followup,
        editable_actor_user_id=editable_actor_user_id,
        writable_case_ids=writable_case_ids,
        permission_mode=permission_mode,
    )


def _report_source_label(source: str) -> str:
    lowered = source.lower()
    if lowered == "legal_ops_ui":
        return "法务业务中台"
    if "dingtalk" in lowered:
        return "真实用户消息"
    if "greytest" in lowered or "sandbox" in lowered:
        return "灰测数据库记录"
    return "系统记录" if source else "来源未标记"


def _value(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _try_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
