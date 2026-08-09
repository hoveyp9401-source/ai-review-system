from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.agent2.performance_knowledge import (
    load_live_performance_evidence,
)
from app.agent2.performance_qa import render_performance_report

PerformanceView = Literal["week", "month"]
PerformanceScopeType = Literal["department", "team", "self"]
PerformanceMode = Literal[
    "summary",
    "explain_new",
    "explain_stock",
    "explain_loss",
    "explain_substantial",
]
PerformanceResultStatus = Literal[
    "success",
    "blocked",
    "clarification_required",
]


@dataclass(frozen=True)
class PerformanceToolResult:
    status: PerformanceResultStatus
    response_text: str
    scope_name: str = ""
    rule_version: str = ""
    available_team_names: tuple[str, ...] = ()
    fact_packet: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


async def query_live_defendant_performance(
    *,
    session: Any,
    user: Any,
    settings: Any,
    anchor_date: date,
    tenant_id: str,
    view: PerformanceView,
    scope_type: PerformanceScopeType,
    team_name: str | None,
    mode: PerformanceMode,
    performance_loader: Callable[..., Any] = (
        load_live_performance_evidence
    ),
) -> PerformanceToolResult:
    """Select one published scope and render only deterministic facts."""

    try:
        evidence = await performance_loader(
            session=session,
            user=user,
            settings=settings,
            anchor_date=anchor_date,
            tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001 - optional read must fail closed
        evidence = None
    facts = (
        evidence.facts
        if evidence is not None
        and isinstance(getattr(evidence, "facts", None), dict)
        else {}
    )
    reports = facts.get("reports")
    report = (
        reports.get(view)
        if isinstance(reports, dict)
        else None
    )
    if not isinstance(report, dict):
        return PerformanceToolResult(
            status="blocked",
            response_text=(
                "当前没有可用的已发布被告绩效结果。"
                "本次没有写入或修改日报，请稍后再试。"
            ),
            error_code="PERFORMANCE_REPORT_UNAVAILABLE",
        )
    scopes = tuple(
        item
        for item in report.get("scopes") or ()
        if isinstance(item, dict)
    )
    candidates = _matching_scopes(
        scopes,
        scope_type=scope_type,
        team_name=team_name,
    )
    if len(candidates) != 1:
        available_teams = tuple(
            sorted(
                {
                    str(item.get("scope_name") or "").strip()
                    for item in scopes
                    if str(item.get("scope_type") or "") == "team"
                    and str(item.get("scope_name") or "").strip()
                }
            )
        )
        requested = str(team_name or "").strip()
        if scope_type == "team":
            response_text = (
                f"没有找到唯一对应的团队“{requested}”。"
                f"当前可查团队：{'、'.join(available_teams) or '暂无'}。"
                "请直接回复完整团队名称；本次没有写入或修改日报。"
            )
        else:
            response_text = (
                "当前无法唯一确定这个绩效查询范围。"
                "请说明要查部门整体、具体团队，还是本人；"
                "本次没有写入或修改日报。"
            )
        return PerformanceToolResult(
            status="clarification_required",
            response_text=response_text,
            available_team_names=available_teams,
            error_code="PERFORMANCE_SCOPE_NOT_UNIQUE",
        )
    scope = candidates[0]
    if str(scope.get("scope_type") or "") == "person_unavailable":
        reason = str(
            scope.get("unavailable_reason")
            or "当前人员与绩效统计范围尚未唯一对应"
        ).strip()
        return PerformanceToolResult(
            status="clarification_required",
            response_text=(
                f"目前还不能可靠计算你的个人被告案件绩效：{reason}。"
                "请先由管理员补齐人员、分公司与团队的对应关系。"
            ),
            scope_name=str(scope.get("scope_name") or ""),
            error_code="PERFORMANCE_PERSON_SCOPE_UNAVAILABLE",
        )
    rule_version = str(facts.get("rule_version") or "")
    return PerformanceToolResult(
        status="success",
        response_text=render_performance_report(
            report=report,
            scope=scope,
            source_status=str(
                facts.get("source_status_label")
                or "当前可用底表"
            ),
            rule_version=rule_version,
            mode=mode,
        ),
        scope_name=str(scope.get("scope_name") or ""),
        rule_version=rule_version,
        fact_packet=_performance_fact_packet(
            report=report,
            scope=scope,
            source_status=str(
                facts.get("source_status_label")
                or "当前可用底表"
            ),
            mode=mode,
        ),
    )


def _matching_scopes(
    scopes: tuple[dict[str, Any], ...],
    *,
    scope_type: PerformanceScopeType,
    team_name: str | None,
) -> tuple[dict[str, Any], ...]:
    if scope_type == "department":
        return tuple(
            item
            for item in scopes
            if str(item.get("scope_type") or "") == "overall"
        )
    if scope_type == "self":
        return tuple(
            item
            for item in scopes
            if str(item.get("scope_type") or "")
            in {"person", "person_unavailable"}
        )
    requested = str(team_name or "").strip()
    return tuple(
        item
        for item in scopes
        if str(item.get("scope_type") or "") == "team"
        and str(item.get("scope_name") or "").strip()
        == requested
    )


def _performance_fact_packet(
    *,
    report: dict[str, Any],
    scope: dict[str, Any],
    source_status: str,
    mode: PerformanceMode = "summary",
) -> dict[str, Any]:
    """Expose selected, already-calculated business facts for natural replies."""

    period = (
        report.get("period")
        if isinstance(report.get("period"), dict)
        else {}
    )
    scope_name = str(scope.get("scope_name") or "").strip()
    scope_type = str(scope.get("scope_type") or "").strip()
    if scope_type == "overall":
        display_scope = "法务部门整体"
    elif scope_type == "person":
        display_scope = "本人"
    else:
        display_scope = scope_name
    definitions = _performance_definitions(
        stock_target=_safe_target(scope.get("stock_target")),
        new_target=_safe_target(scope.get("new_target")),
    )
    include_branches = mode in {"explain_new", "explain_stock"}
    include_new_cases = mode in {"summary", "explain_new"}
    include_closed_cases = mode == "summary"
    new_cases = (
        _safe_case_details(scope.get("period_new_cases"))
        if include_new_cases
        else []
    )
    closed_cases = (
        _safe_case_details(scope.get("period_closed_cases"))
        if include_closed_cases
        else []
    )
    packet = {
        "business_scope": "被告案件绩效",
        "scope_name": display_scope,
        "scope_type": scope_type,
        "period": {
            "view": str(period.get("view") or ""),
            "label": str(period.get("label") or ""),
            "cutoff_date": str(period.get("cutoff_date") or ""),
            "comparison_label": str(
                period.get("comparison_label") or ""
            ),
        },
        "stock_count": _safe_int(scope.get("stock_count")),
        "previous_stock_count": _safe_int(
            scope.get("previous_stock_count")
        ),
        "last_year_stock_count": _safe_int(
            scope.get("last_year_stock_count")
        ),
        "year_to_date_new_count": _safe_int(
            scope.get("year_to_date_new_count")
        ),
        "last_year_to_date_new_count": _safe_int(
            scope.get("last_year_to_date_new_count")
        ),
        "period_new_count": _safe_int(
            scope.get("period_new_count")
        ),
        "period_closed_count": _safe_int(
            scope.get("period_closed_count")
        ),
        "rates": {
            "stock_yoy": _safe_rate(scope.get("stock_yoy")),
            "stock_period_change": _safe_rate(
                scope.get("stock_period_change")
            ),
            "new_yoy": _safe_rate(scope.get("new_yoy")),
        },
        "targets": {
            "stock": _safe_target(scope.get("stock_target")),
            "new": _safe_target(scope.get("new_target")),
        },
        "loss_metrics": _safe_loss_metrics(
            scope.get("loss_metrics")
        ),
        "branches": (
            _safe_branches(scope.get("branches"))
            if include_branches
            else []
        ),
        "period_new_cases": new_cases,
        "period_closed_cases": closed_cases,
        "case_detail_status": {
            "period_new_total": _safe_int(
                scope.get("period_new_case_total")
                if scope.get("period_new_case_total") is not None
                else len(new_cases)
            ),
            "period_new_returned": len(new_cases),
            "period_new_truncated": bool(
                scope.get("period_new_cases_truncated")
            ),
            "period_closed_total": _safe_int(
                scope.get("period_closed_case_total")
                if scope.get("period_closed_case_total") is not None
                else len(closed_cases)
            ),
            "period_closed_returned": len(closed_cases),
            "period_closed_truncated": bool(
                scope.get("period_closed_cases_truncated")
            ),
        },
        "team_summaries": (
            _team_summaries(report)
            if mode == "summary" and scope_type == "overall"
            else []
        ),
        "definitions": definitions,
        "reply_guidance": {
            "answer_scope": (
                "只回答用户这一轮真正问到的内容；只有用户要求完整概览时"
                "才展开全部指标。"
            ),
            "grounding": (
                "只能使用 claim_catalog 中的同一条事实，不得把不同"
                "指标、团队或案件的值互换；不要展示事实编号或内部字段。"
                "系统会逐句核对指标、数值、方向、对象和案件明细。"
            ),
            "follow_up": (
                "结合近期对话理解“这些、哪几件、为什么”等指代，"
                "不要重复上一轮已经回答的标题和无关指标。"
            ),
            "style": (
                "用自然简洁的中文直接作答，不加固定称呼、"
                "内部问答标签或日报免责声明。"
            ),
            "case_list": (
                "列案件时必须原样使用 claim_catalog 中同一条案件事实的"
                "案件名称、分公司、承办法务和日期；不得猜测或拼接。"
            ),
            "selection_contract": (
                "最终回复必须在每个准备采用的事实后附"
                "[依据:claim_catalog中的事实编号]。每行只选择一个事实；"
                "定义、数字、目标和案件不得合并在同一行。系统会根据这些"
                "编号重新生成对用户可见的中文并移除编号，未引用的文字不会"
                "发送给用户。"
            ),
        },
        "data_context": {
            "status": source_status,
            "read_only": True,
            "assignment_error_count": _safe_int(
                report.get("assignment_error_count")
            ),
            "target_conclusions_available": bool(
                report.get(
                    "target_conclusions_available",
                    True,
                )
            ),
        },
    }
    packet["claim_catalog"] = _performance_claim_catalog(
        packet,
        mode=mode,
    )
    packet["available_team_names"] = [
        str(item.get("scope_name") or "")
        for item in packet["team_summaries"]
        if isinstance(item, dict)
        and str(item.get("scope_name") or "").strip()
    ]
    # Collections are represented once in claim_catalog. Keeping a second raw
    # copy would double both model context and the immutable receipt payload.
    packet.pop("branches", None)
    packet.pop("period_new_cases", None)
    packet.pop("period_closed_cases", None)
    packet.pop("team_summaries", None)
    return packet


def _performance_definitions(
    *,
    stock_target: dict[str, str],
    new_target: dict[str, str],
) -> dict[str, str]:
    target_parts: list[str] = []
    stock_display = str(
        stock_target.get("target_display") or ""
    ).strip()
    new_display = str(
        new_target.get("target_display") or ""
    ).strip()
    if stock_display:
        target_parts.append(f"存量同比目标为{stock_display}")
    if new_display:
        target_parts.append(f"新增同比目标为{new_display}")
    target_definition = (
        "，".join(target_parts)
        + "；系统将实际同比变化与各自目标比较后给出是否达到目标，"
        "不由模型自行计算。"
        if target_parts
        else "目标值以当前已发布规则和本次事实为准；缺少目标时不作结论。"
    )
    return {
        "绩效": (
            "当前 Skill 定义的被告案件指标集合，包含存量、"
            "年度累计新增、本周或本月新增与结案、同比下降率、"
            "目标完成情况，以及仅在规则适用时展示的减损指标。"
        ),
        "存量": (
            "截至统计截止日，登记日期不晚于截止日，且该日仍未"
            "结案的被告案件数量。"
        ),
        "新增": (
            "登记日期落在所查询周或月统计区间内的被告案件；"
            "年度累计新增按当年1月1日至截止日统计。"
        ),
        "同比下降率": (
            "系统将当前数量与去年同一截止日数量比较并确定性"
            "计算；回复使用“下降XX%”表达降幅，不显示负号。"
        ),
        "较上期变化": (
            "周维度与上周五比较，月维度与上月末比较；"
            "系统已经确定性计算，不由模型自行运算。"
        ),
        "目标完成": target_definition,
        "团队归属": (
            "按“分公司→法务对接人→法务团队”映射确定，"
            "不直接采用底表中可能存在的团队文字。"
        ),
    }


def _performance_claim_catalog(
    packet: dict[str, Any],
    *,
    mode: PerformanceMode,
) -> dict[str, dict[str, Any]]:
    """Create small, typed facts the reply guard can verify one by one."""

    claims: dict[str, dict[str, Any]] = {}
    scope_name = str(packet.get("scope_name") or "").strip()
    period = (
        packet.get("period")
        if isinstance(packet.get("period"), dict)
        else {}
    )
    view = str(period.get("view") or "")
    period_prefix = "本周" if view == "week" else "本月"

    claims["scope.context"] = {
        "kind": "context",
        "subject": scope_name,
        "entities": [scope_name],
    }
    claims["period.context"] = {
        "kind": "period",
        "label": str(period.get("label") or ""),
        "cutoff_date": str(period.get("cutoff_date") or ""),
        "comparison_label": str(
            period.get("comparison_label") or ""
        ),
        "entities": [scope_name],
    }

    def add_metric(
        claim_id: str,
        *,
        metric_key: str,
        label: str,
        value: Any,
        subject: str = scope_name,
        entities: list[str] | None = None,
        direction: str = "",
        kind: str = "metric",
        status_label: str = "",
    ) -> None:
        if value in (None, "", {}):
            return
        claims[claim_id] = {
            "kind": kind,
            "metric_key": metric_key,
            "label": label,
            "value": value,
            "subject": subject,
            "entities": [
                item
                for item in (entities or [subject])
                if str(item or "").strip()
            ],
            "direction": direction,
            "status_label": status_label,
        }

    add_metric(
        "scope.stock_count",
        metric_key="stock_count",
        label="存量",
        value=packet.get("stock_count"),
    )
    add_metric(
        "scope.previous_stock_count",
        metric_key="previous_stock_count",
        label="上期末存量",
        value=packet.get("previous_stock_count"),
    )
    add_metric(
        "scope.last_year_stock_count",
        metric_key="last_year_stock_count",
        label="去年同期存量",
        value=packet.get("last_year_stock_count"),
    )
    add_metric(
        "scope.year_to_date_new_count",
        metric_key="year_to_date_new_count",
        label="年度累计新增",
        value=packet.get("year_to_date_new_count"),
    )
    add_metric(
        "scope.last_year_to_date_new_count",
        metric_key="last_year_to_date_new_count",
        label="去年同期累计新增",
        value=packet.get("last_year_to_date_new_count"),
    )
    add_metric(
        "scope.period_new_count",
        metric_key="period_new_count",
        label=f"{period_prefix}新增",
        value=packet.get("period_new_count"),
    )
    add_metric(
        "scope.period_closed_count",
        metric_key="period_closed_count",
        label=f"{period_prefix}结案",
        value=packet.get("period_closed_count"),
    )

    rates = (
        packet.get("rates")
        if isinstance(packet.get("rates"), dict)
        else {}
    )
    rate_labels = {
        "stock_yoy": "存量同比",
        "stock_period_change": "存量较上期变化",
        "new_yoy": "新增同比",
    }
    for key, label in rate_labels.items():
        rate = rates.get(key)
        if not isinstance(rate, dict):
            continue
        add_metric(
            f"scope.{key}",
            metric_key=key,
            label=label,
            value={
                "display": str(rate.get("display") or ""),
                "value": str(rate.get("value") or ""),
            },
            direction=str(rate.get("direction") or ""),
            kind="rate",
        )

    targets = (
        packet.get("targets")
        if isinstance(packet.get("targets"), dict)
        else {}
    )
    for key, label in (
        ("stock", "存量同比目标"),
        ("new", "新增同比目标"),
    ):
        target = targets.get(key)
        if not isinstance(target, dict):
            continue
        display = str(target.get("target_display") or "")
        add_metric(
            f"scope.{key}_target",
            metric_key=f"{key}_target",
            label=label,
            value={
                "target_display": display,
                "status_label": str(
                    target.get("status_label") or ""
                ),
            },
            direction=(
                "decline"
                if "下降" in display
                else "growth"
                if "增长" in display or "上升" in display
                else ""
            ),
            kind="target",
            status_label=str(
                target.get("status_label") or ""
            ),
        )

    loss_metrics = (
        packet.get("loss_metrics")
        if isinstance(packet.get("loss_metrics"), dict)
        else {}
    )
    for key, label in (
        ("comprehensive_loss_rate", "综合减损率"),
        ("substantial_loss_amount", "实质减损金额"),
    ):
        metric = loss_metrics.get(key)
        if not isinstance(metric, dict) or not metric:
            continue
        claims[f"scope.{key}"] = {
            "kind": "loss",
            "metric_key": key,
            "label": label,
            "value": dict(metric),
            "subject": scope_name,
            "entities": [scope_name],
        }

    for index, case in enumerate(
        packet.get("period_new_cases") or ()
    ):
        if not isinstance(case, dict):
            continue
        claims[f"new_case.{index}"] = {
            "kind": "case",
            "metric_key": "period_new_count",
            "case_group": "period_new",
            "case_name": str(case.get("case_name") or ""),
            "branch_name": str(case.get("branch_name") or ""),
            "lawyer_name": str(case.get("lawyer_name") or ""),
            "register_date": str(
                case.get("register_date") or ""
            ),
            "close_date": str(case.get("close_date") or ""),
            "entities": [
                item
                for item in (
                    scope_name,
                    case.get("case_name"),
                    case.get("branch_name"),
                    case.get("lawyer_name"),
                )
                if str(item or "").strip()
            ],
        }
    for index, case in enumerate(
        packet.get("period_closed_cases") or ()
    ):
        if not isinstance(case, dict):
            continue
        claims[f"closed_case.{index}"] = {
            "kind": "case",
            "metric_key": "period_closed_count",
            "case_group": "period_closed",
            "case_name": str(case.get("case_name") or ""),
            "branch_name": str(case.get("branch_name") or ""),
            "lawyer_name": str(case.get("lawyer_name") or ""),
            "register_date": str(
                case.get("register_date") or ""
            ),
            "close_date": str(case.get("close_date") or ""),
            "entities": [
                item
                for item in (
                    scope_name,
                    case.get("case_name"),
                    case.get("branch_name"),
                    case.get("lawyer_name"),
                )
                if str(item or "").strip()
            ],
        }

    for index, team in enumerate(packet.get("team_summaries") or ()):
        if not isinstance(team, dict):
            continue
        team_name = str(team.get("scope_name") or "").strip()
        for key, label in (
            ("stock_count", "存量"),
            ("previous_stock_count", "上期末存量"),
            ("last_year_stock_count", "去年同期存量"),
            ("year_to_date_new_count", "年度累计新增"),
            (
                "last_year_to_date_new_count",
                "去年同期累计新增",
            ),
            ("period_new_count", f"{period_prefix}新增"),
            ("period_closed_count", f"{period_prefix}结案"),
        ):
            add_metric(
                f"team.{index}.{key}",
                metric_key=key,
                label=label,
                value=team.get(key),
                subject=team_name,
                entities=[team_name],
            )
        for key, label in (
            ("stock_yoy", "存量同比"),
            ("stock_period_change", "存量较上期变化"),
            ("new_yoy", "新增同比"),
        ):
            rate = team.get(key)
            if not isinstance(rate, dict):
                continue
            add_metric(
                f"team.{index}.{key}",
                metric_key=key,
                label=label,
                value={
                    "display": str(
                        rate.get("display") or ""
                    ),
                    "value": str(rate.get("value") or ""),
                },
                subject=team_name,
                entities=[team_name],
                direction=str(rate.get("direction") or ""),
                kind="rate",
            )
        for key, label in (
            ("stock_target", "存量同比目标"),
            ("new_target", "新增同比目标"),
        ):
            target = team.get(key)
            if not isinstance(target, dict):
                continue
            display = str(
                target.get("target_display") or ""
            )
            add_metric(
                f"team.{index}.{key}",
                metric_key=key,
                label=label,
                value={
                    "target_display": display,
                    "status_label": str(
                        target.get("status_label") or ""
                    ),
                },
                subject=team_name,
                entities=[team_name],
                direction=(
                    "decline"
                    if "下降" in display
                    else "growth"
                    if "增长" in display or "上升" in display
                    else ""
                ),
                kind="target",
                status_label=str(
                    target.get("status_label") or ""
                ),
            )

    for index, branch in enumerate(packet.get("branches") or ()):
        if not isinstance(branch, dict):
            continue
        branch_name = str(
            branch.get("branch_name") or ""
        ).strip()
        lawyer_name = str(
            branch.get("lawyer_name") or ""
        ).strip()
        branch_metrics = (
            (
                ("period_new_count", f"{period_prefix}新增"),
                ("year_to_date_new_count", "年度累计新增"),
            )
            if mode == "explain_new"
            else (("stock_count", "存量"),)
        )
        for key, label in branch_metrics:
            add_metric(
                f"branch.{index}.{key}",
                metric_key=key,
                label=label,
                value=branch.get(key),
                subject=branch_name,
                entities=[scope_name, branch_name, lawyer_name],
            )

    definitions = (
        packet.get("definitions")
        if isinstance(packet.get("definitions"), dict)
        else {}
    )
    required_definition_terms = {
        "绩效": ["被告案件", "指标", "存量", "新增"],
        "存量": ["截止日", "未结案", "被告案件"],
        "新增": ["登记日期", "统计区间", "被告案件"],
        "同比下降率": ["去年", "同一截止日", "比较", "下降"],
        "较上期变化": ["比较", "确定性计算"],
        "团队归属": ["分公司", "法务对接人", "法务团队"],
    }
    required_definition_term_groups = {
        "存量": [
            ["截止日"],
            ["未结案", "没结案", "仍未结案"],
            ["被告案件"],
        ],
        "新增": [
            ["登记日期"],
            ["统计区间", "本周", "本月"],
            ["被告案件"],
        ],
        "同比下降率": [
            ["去年", "同期"],
            ["同一截止日", "同一天", "同期"],
            ["比较", "对比"],
            ["下降", "减少"],
        ],
        "团队归属": [
            ["分公司"],
            ["法务对接人"],
            ["法务团队"],
        ],
    }
    for index, (term, definition) in enumerate(
        definitions.items()
    ):
        claims[f"definition.{index}"] = {
            "kind": "definition",
            "term": str(term),
            "canonical_text": str(definition),
            "required_terms": required_definition_terms.get(
                str(term),
                [],
            ),
            "required_term_groups": (
                required_definition_term_groups.get(
                    str(term),
                    [],
                )
            ),
            "entities": [str(term)],
        }
    return claims


def _safe_rate(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        "display": str(value.get("display") or "").strip(),
        "value": str(value.get("value") or "").strip(),
        "direction": str(value.get("direction") or "").strip(),
    }


def _safe_target(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        "status": str(value.get("status") or "").strip(),
        "status_label": str(
            value.get("status_label") or ""
        ).strip(),
        "target_display": str(
            value.get("target_display") or ""
        ).strip(),
    }


def _safe_loss_metrics(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    allowed_fields = (
        "status",
        "status_label",
        "display",
        "eligible_case_count",
        "claim_amount",
        "payable_amount",
    )
    for key in (
        "comprehensive_loss_rate",
        "substantial_loss_amount",
    ):
        metric = value.get(key)
        if not isinstance(metric, dict):
            continue
        result[key] = {
            field: metric.get(field)
            for field in allowed_fields
            if metric.get(field) is not None
        }
    return result


def _safe_branches(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    fields = (
        "branch_name",
        "lawyer_name",
        "stock_count",
        "previous_stock_count",
        "last_year_stock_count",
        "year_to_date_new_count",
        "last_year_to_date_new_count",
        "period_new_count",
        "period_closed_count",
        "stock_yoy",
        "stock_period_change",
        "new_yoy",
    )
    return [
        {
            field: item.get(field)
            for field in fields
            if item.get(field) is not None
        }
        for item in value[:100]
        if isinstance(item, dict)
        and str(item.get("branch_name") or "").strip()
    ]


def _safe_case_details(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        {
            "case_name": str(item.get("case_name") or "").strip(),
            "branch_name": str(
                item.get("branch_name") or ""
            ).strip(),
            "lawyer_name": str(
                item.get("lawyer_name") or ""
            ).strip(),
            "register_date": str(
                item.get("register_date") or ""
            ).strip(),
            "close_date": str(
                item.get("close_date") or ""
            ).strip(),
        }
        for item in value[:100]
        if isinstance(item, dict)
        and str(item.get("case_name") or "").strip()
    ]


def _team_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in report.get("scopes") or ():
        if (
            not isinstance(item, dict)
            or str(item.get("scope_type") or "") != "team"
        ):
            continue
        result.append(
            {
                "scope_name": str(
                    item.get("scope_name") or ""
                ).strip(),
                "stock_count": _safe_int(
                    item.get("stock_count")
                ),
                "previous_stock_count": _safe_int(
                    item.get("previous_stock_count")
                ),
                "last_year_stock_count": _safe_int(
                    item.get("last_year_stock_count")
                ),
                "year_to_date_new_count": _safe_int(
                    item.get("year_to_date_new_count")
                ),
                "last_year_to_date_new_count": _safe_int(
                    item.get("last_year_to_date_new_count")
                ),
                "period_new_count": _safe_int(
                    item.get("period_new_count")
                ),
                "period_closed_count": _safe_int(
                    item.get("period_closed_count")
                ),
                "stock_yoy": _safe_rate(
                    item.get("stock_yoy")
                ),
                "stock_period_change": _safe_rate(
                    item.get("stock_period_change")
                ),
                "new_yoy": _safe_rate(item.get("new_yoy")),
                "stock_target": _safe_target(
                    item.get("stock_target")
                ),
                "new_target": _safe_target(
                    item.get("new_target")
                ),
            }
        )
    return result


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
