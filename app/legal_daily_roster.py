from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


FORMAL_ROSTER_EFFECTIVE_DATE = date(2026, 8, 22)
FORMAL_ROSTER_MEMBER_COUNT = 74
FORMAL_CHILD_MEMBER_COUNT = 70
FORMAL_CENTER_MEMBER_COUNT = 4
FORMAL_PARENT_DEPARTMENT = "法务合约中心"
FORMAL_CENTER_TEAM_CODE = "legal-center"
# These two people remain management-briefing recipients, but are not the
# complete center-direct roster. Formal membership is always loaded from the
# current effective membership records and is never hard-coded by name.
FORMAL_CENTER_BRIEFING_RECIPIENT_NAMES = frozenset({"赵卫中", "朱佳佳"})
FORMAL_CONFIRMED_TEAM_LEADS = {
    "法务二部": "丁益明",
    "法务四部": "薛旭",
}
# 人员统计归属是独立架构事实，不得从负责人职责反向推导。
FORMAL_CONFIRMED_CENTER_DIRECT_MEMBER_NAMES = frozenset({"丁益明", "薛旭"})
FORMAL_CHILD_TEAM_NAMES = frozenset(
    {
        "法务一部",
        "法务二部",
        "法务三部",
        "法务四部",
        "法务五部",
        "法务六部",
        "综合管理部",
    }
)


@dataclass(frozen=True)
class FormalRosterMember:
    user_id: str
    user_name: str
    dingtalk_user_id: str
    team_id: str
    team_code: str
    team_name: str
    department_name: str
    team_active: bool
    data_complete: bool

    @property
    def center_direct(self) -> bool:
        return self.team_code == FORMAL_CENTER_TEAM_CODE and not self.team_active


@dataclass(frozen=True)
class FormalLegalDailyRoster:
    tenant_id: str
    on_date: date
    members: tuple[FormalRosterMember, ...]

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def child_members(self) -> tuple[FormalRosterMember, ...]:
        return tuple(member for member in self.members if not member.center_direct)

    @property
    def center_members(self) -> tuple[FormalRosterMember, ...]:
        return tuple(member for member in self.members if member.center_direct)

    @property
    def center_member_names(self) -> frozenset[str]:
        return frozenset(member.user_name for member in self.center_members)

    @property
    def user_ids(self) -> tuple[str, ...]:
        return tuple(member.user_id for member in self.members)

    @property
    def dingtalk_user_ids(self) -> tuple[str, ...]:
        return tuple(member.dingtalk_user_id for member in self.members)

    @property
    def team_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(member.team_id for member in self.members))

    def member_by_name(self, name: str) -> FormalRosterMember:
        matches = tuple(member for member in self.members if member.user_name == name)
        if len(matches) != 1:
            raise LookupError(f"formal roster name is not unique: {name}")
        return matches[0]


async def load_formal_legal_daily_roster(
    session: AsyncSession,
    *,
    tenant_id: str,
    on_date: date,
) -> FormalLegalDailyRoster:
    """Load and validate the one formal legal-center roster for a given date."""

    normalized_tenant_id = str(tenant_id or "").strip()
    if not normalized_tenant_id:
        raise ValueError("legal daily roster tenant_id is required")
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT
                    users.id::text AS user_id,
                    users.name AS user_name,
                    users.dingtalk_user_id,
                    users.team_id::text AS user_team_id,
                    teams.id::text AS team_id,
                    teams.code AS team_code,
                    teams.name AS team_name,
                    teams.department_name,
                    teams.active AS team_active,
                    memberships.data_complete
                FROM legal_daily_team_memberships memberships
                JOIN users ON users.id = memberships.user_id
                JOIN teams ON teams.id = memberships.team_id
                WHERE memberships.tenant_id = :tenant_id
                  AND memberships.effective_from <= :on_date
                  AND (
                      memberships.effective_to IS NULL
                      OR memberships.effective_to >= :on_date
                  )
                  AND users.active IS TRUE
                  AND teams.department_name = :parent_department
                  AND (
                      teams.active IS TRUE
                      OR teams.code = :center_team_code
                  )
                ORDER BY teams.active DESC, teams.name, users.name, users.id
                """
                ),
                {
                    "tenant_id": normalized_tenant_id,
                    "on_date": on_date,
                    "parent_department": FORMAL_PARENT_DEPARTMENT,
                    "center_team_code": FORMAL_CENTER_TEAM_CODE,
                },
            )
        )
        .mappings()
        .all()
    )
    members = tuple(_member_from_row(row) for row in rows)
    roster = FormalLegalDailyRoster(
        tenant_id=normalized_tenant_id,
        on_date=on_date,
        members=members,
    )
    _validate_roster(roster, rows=rows)
    return roster


def _member_from_row(row: Any) -> FormalRosterMember:
    return FormalRosterMember(
        user_id=str(row.get("user_id") or "").strip(),
        user_name=str(row.get("user_name") or "").strip(),
        dingtalk_user_id=str(row.get("dingtalk_user_id") or "").strip(),
        team_id=str(row.get("team_id") or "").strip(),
        team_code=str(row.get("team_code") or "").strip(),
        team_name=str(row.get("team_name") or "").strip(),
        department_name=str(row.get("department_name") or "").strip(),
        team_active=bool(row.get("team_active")),
        data_complete=bool(row.get("data_complete")),
    )


def _validate_roster(
    roster: FormalLegalDailyRoster,
    *,
    rows: tuple[Any, ...] | list[Any],
) -> None:
    members = roster.members
    user_ids = [member.user_id for member in members]
    dingtalk_user_ids = [member.dingtalk_user_id for member in members]
    if len(set(user_ids)) != len(user_ids):
        raise RuntimeError("formal roster contains duplicate current memberships")
    if len(set(dingtalk_user_ids)) != len(dingtalk_user_ids):
        raise RuntimeError("formal roster contains duplicate DingTalk identities")
    if any(
        not member.user_id
        or not member.user_name
        or not member.dingtalk_user_id
        or not member.team_id
        or not member.team_name
        for member in members
    ):
        raise RuntimeError("formal roster contains an incomplete identity")
    if any(not member.data_complete for member in members):
        raise RuntimeError("formal roster contains incomplete organization data")
    if roster.on_date < FORMAL_ROSTER_EFFECTIVE_DATE:
        return
    if any(str(row.get("user_team_id") or "").strip() != str(row.get("team_id") or "").strip() for row in rows):
        raise RuntimeError("formal roster contains a system-team mismatch")
    if roster.member_count != FORMAL_ROSTER_MEMBER_COUNT:
        raise RuntimeError(f"expected {FORMAL_ROSTER_MEMBER_COUNT} formal roster members, got {roster.member_count}")
    if len(roster.child_members) != FORMAL_CHILD_MEMBER_COUNT:
        raise RuntimeError(f"expected {FORMAL_CHILD_MEMBER_COUNT} child members, got {len(roster.child_members)}")
    if len(roster.center_members) != FORMAL_CENTER_MEMBER_COUNT:
        raise RuntimeError(
            f"expected {FORMAL_CENTER_MEMBER_COUNT} center-direct members, got {len(roster.center_members)}"
        )
    for member_name in FORMAL_CONFIRMED_CENTER_DIRECT_MEMBER_NAMES:
        matches = tuple(member for member in roster.members if member.user_name == member_name)
        if len(matches) != 1 or not matches[0].center_direct:
            raise RuntimeError(
                f"formal roster placement changed: {member_name} must be center-direct"
            )
    child_team_names = {member.team_name for member in roster.child_members}
    if child_team_names != FORMAL_CHILD_TEAM_NAMES:
        raise RuntimeError(f"formal child departments changed: {sorted(child_team_names)!r}")
    if len({member.team_id for member in roster.child_members}) != len(FORMAL_CHILD_TEAM_NAMES):
        raise RuntimeError("formal roster does not resolve to exactly seven child teams")


def formal_roster_user_ids_for_exact_scope(
    roster: FormalLegalDailyRoster,
    identifiers: Iterable[str],
) -> tuple[str, ...]:
    """Resolve exactly one configured identity for every formal roster member."""

    normalized_identifiers = {str(identifier).strip() for identifier in identifiers if str(identifier).strip()}
    identifier_owners: dict[str, str] = {}
    for member in roster.members:
        for identifier in (member.user_id, member.dingtalk_user_id):
            existing_owner = identifier_owners.get(identifier)
            if existing_owner is not None and existing_owner != member.user_id:
                raise RuntimeError("formal roster contains an ambiguous identity")
            identifier_owners[identifier] = member.user_id

    resolved_user_ids: set[str] = set()
    for identifier in normalized_identifiers:
        user_id = identifier_owners.get(identifier)
        if user_id is None:
            raise RuntimeError("configured scope does not exactly match the formal roster")
        resolved_user_ids.add(user_id)

    if len(normalized_identifiers) != roster.member_count or resolved_user_ids != set(roster.user_ids):
        raise RuntimeError("configured scope does not exactly match the formal roster")
    return roster.user_ids
