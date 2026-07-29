from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
