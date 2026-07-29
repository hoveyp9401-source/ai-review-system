from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from typing import Any

from sqlalchemy import String, cast, select

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.context_pack import Agent2ContextPack, KnowledgeEvidenceFrame
from app.db import AsyncSessionLocal
from app.legal_ops_data_intake.service import DataIntakeError, DataIntakeService
from app.models import User

logger = logging.getLogger(__name__)
_SCOPE_FIELDS = (
    "scope_key",
    "scope_name",
    "scope_type",
    "stock_count",
    "previous_stock_count",
    "last_year_stock_count",
    "stock_yoy",
    "stock_period_change",
    "year_to_date_new_count",
    "last_year_to_date_new_count",
    "new_yoy",
    "period_new_count",
    "period_closed_count",
    "stock_target",
    "new_target",
    "loss_metrics",
)


async def attach_live_performance_catalog(
    *,
    context_pack: Agent2ContextPack,
    session: Any,
    user: Any,
    settings: Any,
    decision: Any,
    anchor_date: date,
    tenant_id: str = "",
    actor_role_ids: tuple[str, ...] = (),
    service_factory: Callable[..., Any] = DataIntakeService,
    scope_repository_factory: Callable[..., Any] | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> Agent2ContextPack:
    """Attach a compact read-only catalog only after semantic internal-QA admission."""

    if not _decision_has_internal_query(decision):
        return context_pack
    # Product policy: aggregate defendant performance is internal-public to
    # every authenticated legal user. Personal scopes remain self-only.
    del actor_role_ids, scope_repository_factory
    evidence = await load_live_performance_evidence(
        session=session,
        user=user,
        settings=settings,
        anchor_date=anchor_date,
        tenant_id=tenant_id,
        service_factory=service_factory,
        session_factory=session_factory,
    )
    if evidence is None:
        return context_pack
    retained = tuple(
        item
        for item in context_pack.knowledge
        if item.source_type != "performance_report_catalog"
    )
    return replace(context_pack, knowledge=retained + (evidence,))


async def load_live_performance_evidence(
    *,
    session: Any,
    user: Any,
    settings: Any,
    anchor_date: date,
    tenant_id: str = "",
    service_factory: Callable[..., Any] = DataIntakeService,
    session_factory: Callable[[], Any] | None = None,
) -> KnowledgeEvidenceFrame | None:
    """Load one permission-filtered, published-only performance catalog."""

    if not bool(getattr(settings, "agent2_performance_knowledge_enabled", False)):
        return None
    if not bool(getattr(settings, "legal_ops_data_intake_enabled", False)):
        return None
    resolved_tenant = str(
        tenant_id or getattr(settings, "legal_ops_live_tenant_id", "") or ""
    ).strip()
    if not resolved_tenant:
        return None
    try:
        loader = _load_catalog_and_identity(
            session=session,
            user=user,
            settings=settings,
            tenant_id=resolved_tenant,
            anchor_date=anchor_date,
            service_factory=service_factory,
            session_factory=session_factory,
        )
        catalog, personal_name = await asyncio.wait_for(
            loader,
            timeout=float(
                getattr(
                    settings,
                    "agent2_performance_knowledge_timeout_seconds",
                    8.0,
                )
                or 8.0
            ),
        )
    except TimeoutError:
        logger.info("performance catalog unavailable: timeout")
        return None
    except (DataIntakeError, ValueError, KeyError, TypeError):
        return None
    except Exception as exc:  # noqa: BLE001 - optional knowledge must not break a turn
        logger.info(
            "performance catalog unavailable: %s",
            exc.__class__.__name__,
        )
        return None
    if not isinstance(catalog, dict):
        return None
    return _catalog_evidence(
        catalog,
        allow_department=True,
        allowed_team_names=set(),
        personal_name=personal_name,
        personal_label=(
            personal_name or str(getattr(user, "name", "") or "").strip()
        ),
        anchor_date=anchor_date,
    )


async def _load_catalog_and_identity(
    *,
    session: Any,
    user: Any,
    settings: Any,
    tenant_id: str,
    anchor_date: date,
    service_factory: Callable[..., Any],
    session_factory: Callable[[], Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    async def load(read_session: Any) -> tuple[dict[str, Any] | None, str]:
        service = service_factory(
            read_session,
            settings,
            tenant_id=tenant_id,
            actor_user_id=str(getattr(user, "id", "") or ""),
            code_version=os.getenv("APP_COMMIT_SHA", "workspace"),
        )
        catalog = await service.current_performance_report_catalog(
            anchor_date=anchor_date
        )
        personal_name = ""
        try:
            personal_name = await _unique_active_user_name(
                read_session,
                tenant_id=tenant_id,
                actor_user_id=str(getattr(user, "id", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - personal data must fail closed
            logger.info(
                "performance personal identity unavailable: %s",
                exc.__class__.__name__,
            )
        return catalog, personal_name

    independent_factory = session_factory
    if independent_factory is None and service_factory is DataIntakeService:
        independent_factory = AsyncSessionLocal
    if independent_factory is None:
        return await load(session)
    async with independent_factory() as read_session:
        return await load(read_session)


def _catalog_evidence(
    catalog: dict[str, Any],
    *,
    allow_department: bool,
    allowed_team_names: set[str],
    personal_name: str,
    personal_label: str,
    anchor_date: date,
) -> KnowledgeEvidenceFrame | None:
    raw_reports: dict[str, dict[str, Any]] = {}
    for view in ("week", "month"):
        raw_report = catalog.get(view)
        if not isinstance(raw_report, dict):
            nested = catalog.get("reports")
            raw_report = nested.get(view) if isinstance(nested, dict) else None
        if not isinstance(raw_report, dict):
            continue
        raw_reports[view] = raw_report
    authorized_personal_name = (
        personal_name
        if personal_name
        and raw_reports
        and all(
            _matching_person_scope_count(report, personal_name) == 1
            for report in raw_reports.values()
        )
        else ""
    )
    reports: dict[str, dict[str, Any]] = {}
    for view, raw_report in raw_reports.items():
        compact = _compact_report(
            raw_report,
            allow_department=allow_department,
            allowed_team_names=allowed_team_names,
            personal_name=authorized_personal_name,
            personal_label=personal_label,
        )
        if compact["scopes"]:
            reports[view] = compact
    if not reports:
        return None
    rule_version = str(catalog.get("rule_version") or "").strip()
    status_label = str(catalog.get("data_status_label") or "当前可用底表").strip()
    return KnowledgeEvidenceFrame(
        source_type="performance_report_catalog",
        source_id=f"defendant-performance:{anchor_date.isoformat()}:{rule_version or 'current'}",
        title="被告案件绩效指标",
        summary=f"依据{status_label}和已确认规则确定性计算的周/月指标。",
        facts={
            "business_scope": "被告案件绩效",
            "source_status_label": status_label,
            "rule_version": rule_version,
            "reports": reports,
            "read_only": True,
        },
        confidence=1.0,
        freshness=anchor_date.isoformat(),
    )


def _compact_report(
    report: dict[str, Any],
    *,
    allow_department: bool,
    allowed_team_names: set[str],
    personal_name: str,
    personal_label: str,
) -> dict[str, Any]:
    scopes = []
    all_scopes = [
        *(report.get("scopes") or ()),
        *(report.get("personal_scopes") or ()),
    ]
    personal_candidates = []
    for raw_scope in all_scopes:
        if not isinstance(raw_scope, dict):
            continue
        key = str(raw_scope.get("scope_key") or "")
        name = str(raw_scope.get("scope_name") or "")
        scope_type = str(raw_scope.get("scope_type") or "")
        if scope_type == "person":
            if personal_name and name == personal_name:
                personal_candidates.append(raw_scope)
            continue
        else:
            permitted = allow_department or (
                key in allowed_team_names or name in allowed_team_names
            )
        if not permitted:
            continue
        scopes.append(_compact_scope_payload(raw_scope))
    if len(personal_candidates) == 1:
        scopes.append(_compact_scope_payload(personal_candidates[0]))
    elif personal_label:
        scopes.append(
            {
                "scope_key": "__self_unavailable__",
                "scope_name": personal_label,
                "scope_type": "person_unavailable",
                "unavailable_reason": (
                    "当前规则中的分公司到法务对接人关系尚未精确匹配到本人"
                ),
            }
        )
    period = report.get("period")
    return {
        "period": dict(period) if isinstance(period, dict) else {},
        "scopes": scopes,
        "assignment_error_count": int(
            report.get("assignment_error_count")
            or len(report.get("assignment_errors") or ())
        ),
        "data_quality_error_count": int(
            report.get("data_quality_error_count")
            or len(report.get("data_quality_errors") or ())
        ),
        "target_conclusions_available": bool(
            report.get("target_conclusions_available", True)
        ),
        "source_row_count": int(report.get("source_row_count") or 0),
        "mapped_row_count": int(report.get("mapped_row_count") or 0),
    }


def _compact_scope_payload(raw_scope: dict[str, Any]) -> dict[str, Any]:
    compact_scope = {field: raw_scope.get(field) for field in _SCOPE_FIELDS}
    compact_scope["branches"] = [
        {
            field: branch.get(field)
            for field in (
                "branch_name",
                "stock_count",
                "last_year_stock_count",
                "stock_yoy",
                "stock_period_change",
                "year_to_date_new_count",
                "last_year_to_date_new_count",
                "new_yoy",
                "period_new_count",
                "period_closed_count",
            )
        }
        for branch in raw_scope.get("branches") or ()
        if isinstance(branch, dict)
    ]
    return compact_scope


async def _unique_active_user_name(
    session: Any,
    *,
    tenant_id: str,
    actor_user_id: str,
) -> str:
    """Resolve a personal scope only through a unique active tenant identity."""

    if not tenant_id or not actor_user_id:
        return ""
    result = await session.execute(
        select(User.id, User.name)
        .join(
            Agent2IdentityBinding,
            Agent2IdentityBinding.user_id == cast(User.id, String),
        )
        .where(
            Agent2IdentityBinding.tenant_id == tenant_id,
            Agent2IdentityBinding.active.is_(True),
            User.active.is_(True),
        )
    )
    rows = [
        (str(user_id or "").strip(), str(name or "").strip())
        for user_id, name in result.all()
    ]
    actor_rows = [row for row in rows if row[0] == actor_user_id]
    if len(actor_rows) != 1:
        return ""
    actor_name = actor_rows[0][1]
    if not actor_name:
        return ""
    if sum(name == actor_name for _user_id, name in rows) != 1:
        return ""
    return actor_name


def _matching_person_scope_count(report: dict[str, Any], personal_name: str) -> int:
    return sum(
        1
        for raw_scope in (
            *(report.get("scopes") or ()),
            *(report.get("personal_scopes") or ()),
        )
        if isinstance(raw_scope, dict)
        and str(raw_scope.get("scope_type") or "") == "person"
        and str(raw_scope.get("scope_name") or "") == personal_name
    )


def _decision_has_internal_query(decision: Any) -> bool:
    return any(
        "internal_query" in tuple(getattr(segment, "intents", ()) or ())
        for segment in tuple(getattr(decision, "segments", ()) or ())
    )
