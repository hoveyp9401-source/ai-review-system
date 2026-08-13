"""Owner-scoped SQL read adapter for weekly-plan history suggestions."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.daily_state import DRAFT_ITEM_IDS_KEY
from app.agent2.weekly_plan_history_pipeline import (
    HistorySuggestionRefreshRequest,
    HistorySuggestionResult,
    TrustedHistoryWindow,
)
from app.agent2.weekly_plan_history_suggestions import (
    ConfirmedRecordState,
    StableTomorrowPlanItem,
    TrustedPersonalRecord,
)
from app.agent2.weekly_plan_store import _plans, _suggestion_row, _suggestions
from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    TrustedSourceKind,
    TrustedSuggestionEvidence,
    WeeklyPlanSuggestion,
)
from app.models import DailyReport


class SqlTrustedDailyHistorySource:
    """Read only authenticated-owner Daily rows needed for bounded review."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def load_history(
        self, request: HistorySuggestionRefreshRequest
    ) -> TrustedHistoryWindow:
        try:
            owner_id = UUID(request.owner_user_id)
        except ValueError as exc:
            raise ValueError("owner_user_id must be a UUID") from exc
        source_week_start = request.target_week_start - timedelta(days=7)
        rows = (
            await self._session.execute(
                select(DailyReport)
                .where(
                    exists(
                        select(1).where(
                            Agent2IdentityBinding.tenant_id
                            == request.tenant_id,
                            Agent2IdentityBinding.user_id
                            == request.owner_user_id,
                            Agent2IdentityBinding.active.is_(True),
                        )
                    ),
                    DailyReport.user_id == owner_id,
                    DailyReport.report_date >= source_week_start,
                    DailyReport.report_date < request.target_week_start,
                    DailyReport.status == "completed",
                )
                .order_by(DailyReport.report_date, DailyReport.updated_at)
            )
        ).scalars().all()
        trusted = tuple(row for row in rows if _trusted_record_state(row) is not None)
        source_items = tuple(
            item
            for row in trusted
            for item in _stable_tomorrow_items(row, request=request)
        )
        later_records = tuple(
            record
            for row in trusted
            if (record := _trusted_personal_record(row, request=request)) is not None
        )
        return TrustedHistoryWindow(
            source_items=source_items,
            later_records=later_records,
        )


class SqlHistorySuggestionStore:
    """Persist only reviewed suggestion state inside one owner/plan row lock."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def _load_plan_row(
        self,
        request: HistorySuggestionRefreshRequest,
        *,
        for_update: bool,
    ):
        statement = select(_plans).where(
            _plans.c.tenant_id == request.tenant_id,
            _plans.c.owner_user_id == request.owner_user_id,
            _plans.c.target_week_start == request.target_week_start,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).mappings().one_or_none()

    async def load_available(self, request: HistorySuggestionRefreshRequest):
        plan_row = await self._load_plan_row(request, for_update=False)
        if plan_row is None:
            return ()
        rows = (
            await self._session.execute(
                select(_suggestions)
                .where(
                    _suggestions.c.tenant_id == request.tenant_id,
                    _suggestions.c.plan_id == plan_row["plan_id"],
                    _suggestions.c.owner_user_id == request.owner_user_id,
                    _suggestions.c.target_week_start
                    == request.target_week_start,
                    _suggestions.c.status.in_(
                        ("available", "accepted", "rejected")
                    ),
                )
                .order_by(_suggestions.c.created_at, _suggestions.c.suggestion_id)
            )
        ).mappings().all()
        return tuple(_suggestion_from_row(row) for row in rows)

    async def persist(
        self,
        request: HistorySuggestionRefreshRequest,
        result: HistorySuggestionResult,
    ) -> HistorySuggestionResult:
        plan_row = await self._load_plan_row(request, for_update=True)
        if plan_row is None:
            return HistorySuggestionResult(new_suggestions=())
        plan_id = plan_row["plan_id"]
        persisted_new = []
        for suggestion in result.new_suggestions:
            _require_suggestion_scope(suggestion, request=request)
            row = _suggestion_row(
                SimpleNamespace(
                    tenant_id=request.tenant_id,
                    plan_id=str(plan_id),
                ),
                suggestion,
            )
            persisted = (
                await self._session.execute(
                    pg_insert(_suggestions)
                    .values(**row)
                    .on_conflict_do_nothing()
                    .returning(*_suggestions.c)
                )
            ).mappings().one_or_none()
            if persisted is not None:
                persisted_new.append(_suggestion_from_row(persisted))
        persisted_superseded = []
        for suggestion in result.superseded_suggestions:
            _require_suggestion_scope(suggestion, request=request)
            persisted = (
                await self._session.execute(
                _suggestions.update()
                .where(
                    _suggestions.c.tenant_id == request.tenant_id,
                    _suggestions.c.plan_id == plan_id,
                    _suggestions.c.owner_user_id == request.owner_user_id,
                    _suggestions.c.suggestion_id == suggestion.suggestion_id,
                    _suggestions.c.status == "available",
                )
                .values(
                    status="superseded",
                    decided_at=suggestion.decided_at,
                    superseded_by_id=suggestion.superseded_by_id,
                )
                .returning(*_suggestions.c)
                )
            ).mappings().one_or_none()
            if persisted is not None:
                persisted_superseded.append(_suggestion_from_row(persisted))
        await self._session.flush()
        return HistorySuggestionResult(
            new_suggestions=tuple(persisted_new),
            superseded_suggestions=tuple(persisted_superseded),
        )


def _require_suggestion_scope(
    suggestion: WeeklyPlanSuggestion,
    *,
    request: HistorySuggestionRefreshRequest,
) -> None:
    if (
        suggestion.owner_user_id != request.owner_user_id
        or suggestion.target_week_start != request.target_week_start
        or suggestion.source_kind is not TrustedSourceKind.CONFIRMED_RECORD
    ):
        raise ValueError("weekly_plan_history_suggestion_scope_mismatch")


def _suggestion_from_row(row: Any) -> WeeklyPlanSuggestion:
    return WeeklyPlanSuggestion(
        suggestion_id=str(row["suggestion_id"]),
        owner_user_id=str(row["owner_user_id"]),
        target_week_start=row["target_week_start"],
        evidence=TrustedSuggestionEvidence(
            owner_user_id=str(row["owner_user_id"]),
            source_kind=TrustedSourceKind(str(row["source_kind"])),
            source_ref=str(row["source_ref"]),
            source_version=str(row["source_version"]),
            evidence_text=str(row["evidence_text"]),
            evidence_sha256=str(row["evidence_sha256"]),
        ),
        matter_excerpt=str(row["matter_excerpt"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        status=SuggestionStatus(str(row["status"])),
        decision_ref=row["decision_ref"],
        accepted_item_id=(
            str(row["accepted_item_id"])
            if row["accepted_item_id"] is not None
            else None
        ),
        decided_at=row["decided_at"],
        superseded_by_id=row["superseded_by_id"],
    )


def _stable_tomorrow_items(
    row: Any, *, request: HistorySuggestionRefreshRequest
) -> tuple[StableTomorrowPlanItem, ...]:
    recorded_at = _recorded_at(row)
    if recorded_at > request.as_of:
        return ()
    values = _clean_items(getattr(row, "tomorrow_plan", None))
    if not values:
        return ()
    item_ids = _tomorrow_item_ids(row, values)
    source_text = _exact_record_text(row)
    source_ref = f"daily-report:{row.id}"
    version = _source_version(row)
    state = _trusted_record_state(row)
    if state is None:
        return ()
    return tuple(
        StableTomorrowPlanItem(
            owner_user_id=request.owner_user_id,
            field_name="tomorrow_plan",
            item_ref=item_ids[index],
            source_ref=source_ref,
            source_version=version,
            record_state=state,
            recorded_at=recorded_at,
            source_text=source_text,
            source_sha256=_sha256(source_text),
            item_exact_text=value,
        )
        for index, value in enumerate(values)
    )


def _trusted_personal_record(
    row: Any, *, request: HistorySuggestionRefreshRequest
) -> TrustedPersonalRecord | None:
    recorded_at = _recorded_at(row)
    if recorded_at > request.as_of:
        return None
    evidence_text = _exact_record_text(row)
    return TrustedPersonalRecord(
        owner_user_id=request.owner_user_id,
        source_kind=TrustedSourceKind.CONFIRMED_RECORD,
        source_ref=f"daily-report:{row.id}",
        source_version=_source_version(row),
        recorded_at=recorded_at,
        evidence_text=evidence_text,
        evidence_sha256=_sha256(evidence_text),
    )


def _trusted_record_state(row: Any) -> ConfirmedRecordState | None:
    if str(getattr(row, "status", "") or "") != "completed":
        return None
    if bool(getattr(row, "confirmed_by_user", False)) or str(
        getattr(row, "confirmation_type", "") or ""
    ) in {"user_confirmed", "admin_confirmed"}:
        return ConfirmedRecordState.CONFIRMED
    if str(getattr(row, "confirmation_type", "") or "") == "auto_submitted_timeout":
        return ConfirmedRecordState.SUBMITTED
    return None


def _tomorrow_item_ids(row: Any, values: tuple[str, ...]) -> tuple[str, ...]:
    section_status = getattr(row, "section_status", None)
    section_status = section_status if isinstance(section_status, dict) else {}
    draft_ids = section_status.get(DRAFT_ITEM_IDS_KEY)
    draft_ids = draft_ids if isinstance(draft_ids, dict) else {}
    raw_ids = draft_ids.get("tomorrow_plan")
    raw_ids = raw_ids if isinstance(raw_ids, list) else []
    return tuple(
        str(raw_ids[index]).strip()
        if index < len(raw_ids) and str(raw_ids[index]).strip()
        else f"tomorrow-plan:{index + 1}:{_sha256(value)[:16]}"
        for index, value in enumerate(values)
    )


def _source_version(row: Any) -> str:
    section_status = getattr(row, "section_status", None)
    section_status = section_status if isinstance(section_status, dict) else {}
    explicit = section_status.get("_agent2_report_version")
    if isinstance(explicit, int) and explicit >= 0:
        return str(explicit)
    return _sha256(_exact_record_text(row))


def _recorded_at(row: Any) -> datetime:
    value = getattr(row, "submitted_at", None) or getattr(row, "updated_at", None)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        report_date = getattr(row, "report_date", None)
        if not isinstance(report_date, date):
            raise ValueError("trusted Daily record must have an aware timestamp")
        raise ValueError("trusted Daily record timestamp must include timezone")
    return value


def _exact_record_text(row: Any) -> str:
    return json.dumps(
        {
            "today_work": list(_clean_items(getattr(row, "today_work", None))),
            "problems": list(_clean_items(getattr(row, "problems", None))),
            "tomorrow_plan": list(
                _clean_items(getattr(row, "tomorrow_plan", None))
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _clean_items(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(str(value).strip() for value in values if str(value).strip())


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["SqlHistorySuggestionStore", "SqlTrustedDailyHistorySource"]
