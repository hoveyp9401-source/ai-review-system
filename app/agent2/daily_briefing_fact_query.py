from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID
import unicodedata

from sqlalchemy import select

from app.legal_daily_dashboard.domain import DailyReportRecord, MemberRecord
from app.legal_daily_dashboard.sql_repository import SqlDashboardRepository
from app.models import ReportInteractionEvent


_BRIEFING_ACTIONS = (
    "daily_briefing_sent",
    "daily_briefing_delivery_pending",
    "daily_briefing_failed",
)
_LEGACY_SNAPSHOT_LIMIT = (
    "该历史晨报没有保存生成时的结构化成员分类快照，不能仅凭当前日报状态反推当时原因。"
)
_NO_RECORDED_BRIEFING_LIMIT = (
    "没有找到与该日期和范围匹配的晨报发送记录，无法确认系统当时发送了什么。"
)


@dataclass(frozen=True)
class DailyBriefingFactEvent:
    recipient_ref: str
    recipient_name: str
    report_date: date
    created_at: datetime
    scope: str
    team_name: str
    department_name: str
    message_status: str
    delivery_verified: bool | None
    message_text: str
    briefing_snapshot: dict[str, Any] | None


@dataclass(frozen=True)
class DailyBriefingFactSource:
    members: tuple[MemberRecord, ...] = ()
    reports: tuple[DailyReportRecord, ...] = ()
    events: tuple[DailyBriefingFactEvent, ...] = ()


@dataclass(frozen=True)
class DailyBriefingFactQueryRequest:
    report_date: date
    view: str = "member_classification"
    member_name: str | None = None
    recipient_name: str | None = None
    team_name: str | None = None


class DailyBriefingFactRepository(Protocol):
    async def load_source(
        self,
        *,
        tenant_id: str,
        report_date: date,
    ) -> DailyBriefingFactSource: ...


class DailyBriefingFactNotFound(LookupError):
    pass


class DailyBriefingFactAmbiguous(LookupError):
    def __init__(self, candidates: tuple[dict[str, str], ...]) -> None:
        super().__init__("daily briefing target is ambiguous")
        self.candidates = candidates


class DailyBriefingFactQuery:
    """Read one historical briefing from recorded outbound facts.

    The module deliberately returns evidence and its limits. It never turns a
    later report state into an explanation for an earlier briefing.
    """

    def __init__(self, repository: DailyBriefingFactRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        request: DailyBriefingFactQueryRequest,
        now: datetime,
    ) -> dict[str, Any]:
        del now
        source = await self._repository.load_source(
            tenant_id=tenant_id,
            report_date=request.report_date,
        )
        target_member = (
            _resolve_optional_member(
                source.members,
                requested_name=request.member_name,
                default_ref=(
                    actor_user_id if request.member_name is None else None
                ),
            )
            if request.view == "member_classification"
            else None
        )
        recipient = _resolve_optional_member(
            source.members,
            requested_name=request.recipient_name,
            default_ref=(
                actor_user_id
                if request.view == "recipient_delivery"
                and request.recipient_name is None
                else None
            ),
        )
        team_name = _resolve_optional_team_name(
            source,
            request.team_name,
        )
        events = tuple(
            event
            for event in source.events
            if _event_matches(
                event,
                target_member=target_member,
                recipient=recipient,
                team_name=team_name,
                actor_user_id=actor_user_id,
                has_explicit_scope=bool(
                    request.member_name
                    or request.recipient_name
                    or request.team_name
                ),
            )
        )
        events = tuple(sorted(events, key=lambda item: item.created_at))
        recorded = [
            _safe_event(event, target_member=target_member)
            for event in events
        ]
        limits: list[str] = []
        if not recorded:
            limits.append(_NO_RECORDED_BRIEFING_LIMIT)
        elif any(not item["snapshot_available"] for item in recorded):
            limits.append(_LEGACY_SNAPSHOT_LIMIT)
        current_submission = _current_submission(
            source.reports,
            target_member=target_member,
            report_date=request.report_date,
        )
        return {
            "query_kind": "daily_briefing_facts",
            "report_date": request.report_date.isoformat(),
            "target_member": (
                _safe_member(target_member)
                if target_member is not None
                else None
            ),
            "target_recipient": (
                _safe_member(recipient)
                if recipient is not None
                else None
            ),
            "target_team_name": team_name,
            "recorded_briefings": recorded,
            "current_submission": current_submission,
            "cause": None,
            "evidence_limits": limits,
        }


class SqlDailyBriefingFactRepository:
    """Tenant-filtered SQL source for the briefing fact module."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._dashboard = SqlDashboardRepository(session)

    async def load_source(
        self,
        *,
        tenant_id: str,
        report_date: date,
    ) -> DailyBriefingFactSource:
        records = await self._dashboard.load_records(
            tenant_id=tenant_id,
            team_refs=None,
            start_date=report_date,
            end_date=report_date,
        )
        member_by_uuid: dict[UUID, MemberRecord] = {}
        for member in records.members:
            try:
                member_by_uuid[UUID(str(member.ref))] = member
            except (TypeError, ValueError, AttributeError):
                continue
        if not member_by_uuid:
            return DailyBriefingFactSource(
                members=records.members,
                reports=records.reports,
            )
        rows = tuple(
            (
                await self._session.scalars(
                    select(ReportInteractionEvent)
                    .where(
                        ReportInteractionEvent.user_id.in_(
                            tuple(member_by_uuid)
                        ),
                        ReportInteractionEvent.report_date == report_date,
                        ReportInteractionEvent.backend_action.in_(
                            _BRIEFING_ACTIONS
                        ),
                    )
                    .order_by(ReportInteractionEvent.created_at)
                )
            ).all()
        )
        events: list[DailyBriefingFactEvent] = []
        for row in rows:
            decision = dict(row.llm_decision_json or {})
            snapshot = decision.get("briefing_snapshot")
            if not isinstance(snapshot, dict):
                snapshot = None
            recipient = member_by_uuid.get(row.user_id)
            if recipient is None:
                continue
            delivery_value = decision.get("delivery_verified")
            events.append(
                DailyBriefingFactEvent(
                    recipient_ref=str(recipient.ref),
                    recipient_name=recipient.name,
                    report_date=row.report_date,
                    created_at=row.created_at,
                    scope=str(decision.get("scope") or ""),
                    team_name=str(decision.get("team_name") or ""),
                    department_name=str(
                        decision.get("department_name") or ""
                    ),
                    message_status=str(
                        decision.get("message_status") or "unknown"
                    ),
                    delivery_verified=(
                        bool(delivery_value)
                        if delivery_value is not None
                        else None
                    ),
                    message_text=str(row.message_text or ""),
                    briefing_snapshot=snapshot,
                )
            )
        return DailyBriefingFactSource(
            members=records.members,
            reports=records.reports,
            events=tuple(events),
        )


def _resolve_optional_member(
    members: tuple[MemberRecord, ...],
    *,
    requested_name: str | None,
    default_ref: str | None,
) -> MemberRecord | None:
    if requested_name is not None:
        candidates = tuple(
            member
            for member in members
            if _same_text(member.name, requested_name)
        )
    elif default_ref is not None:
        candidates = tuple(
            member for member in members if str(member.ref) == default_ref
        )
    else:
        return None
    if not candidates:
        raise DailyBriefingFactNotFound("briefing person not found")
    if len(candidates) > 1:
        raise DailyBriefingFactAmbiguous(
            tuple(
                {
                    "name": member.name,
                    "team_name": member.team_name,
                }
                for member in candidates
            )
        )
    return candidates[0]


def _resolve_optional_team_name(
    source: DailyBriefingFactSource,
    requested_name: str | None,
) -> str | None:
    if requested_name is None:
        return None
    candidates = tuple(
        dict.fromkeys(
            value
            for value in (
                *(member.team_name for member in source.members),
                *(event.team_name for event in source.events),
            )
            if value and _same_text(value, requested_name)
        )
    )
    if not candidates:
        raise DailyBriefingFactNotFound("briefing team not found")
    if len(candidates) > 1:
        raise DailyBriefingFactAmbiguous(
            tuple({"team_name": value} for value in candidates)
        )
    return candidates[0]


def _event_matches(
    event: DailyBriefingFactEvent,
    *,
    target_member: MemberRecord | None,
    recipient: MemberRecord | None,
    team_name: str | None,
    actor_user_id: str,
    has_explicit_scope: bool,
) -> bool:
    if recipient is not None and event.recipient_ref != str(recipient.ref):
        return False
    if team_name is not None:
        return event.scope == "team" and _same_text(
            event.team_name,
            team_name,
        )
    if target_member is not None:
        if _snapshot_has_member(
            event.briefing_snapshot,
            target_member,
        ):
            return True
        if event.briefing_snapshot is not None:
            return False
        return bool(
            event.scope == "department"
            or _same_text(event.team_name, target_member.team_name)
            or event.recipient_ref == str(target_member.ref)
        )
    if recipient is not None:
        return True
    if not has_explicit_scope:
        return event.recipient_ref == actor_user_id
    return True


def _safe_event(
    event: DailyBriefingFactEvent,
    *,
    target_member: MemberRecord | None,
) -> dict[str, Any]:
    snapshot = event.briefing_snapshot
    member_snapshot = _snapshot_member(snapshot, target_member)
    return {
        "sent_at": event.created_at.isoformat(),
        "scope": event.scope,
        "team_name": event.team_name or None,
        "department_name": event.department_name or None,
        "recipient_name": event.recipient_name,
        "message_status": event.message_status,
        "delivery_verified": event.delivery_verified,
        "message_text": event.message_text,
        "snapshot_available": snapshot is not None,
        "snapshot_generated_at": (
            str(snapshot.get("generated_at") or "") or None
            if snapshot is not None
            else None
        ),
        "member_at_snapshot": member_snapshot,
    }


def _snapshot_has_member(
    snapshot: dict[str, Any] | None,
    member: MemberRecord,
) -> bool:
    return _snapshot_member(snapshot, member) is not None


def _snapshot_member(
    snapshot: dict[str, Any] | None,
    member: MemberRecord | None,
) -> dict[str, Any] | None:
    if snapshot is None or member is None:
        return None
    rows = snapshot.get("members")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("member_ref") or "") != str(member.ref) and not (
            row.get("member_name")
            and _same_text(str(row["member_name"]), member.name)
        ):
            continue
        return {
            "member_name": str(row.get("member_name") or member.name),
            "team_name": str(row.get("team_name") or member.team_name),
            "classification": str(row.get("classification") or "unknown"),
            "report_status": row.get("report_status"),
            "confirmation_type": row.get("confirmation_type"),
            "submitted_at": row.get("submitted_at"),
        }
    return None


def _current_submission(
    reports: tuple[DailyReportRecord, ...],
    *,
    target_member: MemberRecord | None,
    report_date: date,
) -> dict[str, Any] | None:
    if target_member is None:
        return None
    report = next(
        (
            item
            for item in reports
            if str(item.member_ref) == str(target_member.ref)
            and item.report_date == report_date
        ),
        None,
    )
    if report is None:
        return {
            "report_exists": False,
            "status": None,
            "confirmation_type": None,
            "confirmed_by_user": False,
            "submitted_at": None,
        }
    return {
        "report_exists": True,
        "status": report.status,
        "confirmation_type": report.confirmation_type,
        "confirmed_by_user": report.confirmed_by_user,
        "submitted_at": (
            report.submitted_at.isoformat()
            if report.submitted_at is not None
            else None
        ),
    }


def _safe_member(member: MemberRecord) -> dict[str, str]:
    return {
        "name": member.name,
        "team_name": member.team_name,
        "department_name": member.department_name,
    }


def _same_text(left: str, right: str) -> bool:
    return _normalized_text(left) == _normalized_text(right)


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
