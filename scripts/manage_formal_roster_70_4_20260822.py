from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.db import AsyncSessionLocal, engine


SCHEMA_VERSION = "legal.formal_roster_70_4.v1"
MIGRATION_KEY = "formal-roster-70-4-20260822"
EFFECTIVE_DATE = date(2026, 8, 22)
CENTER_TEAM_CODE = "legal-center"
PARENT_DEPARTMENT = "法务合约中心"
TARGET_TEAMS = {
    "丁益明": "monthly-law-2",
    "薛旭": "monthly-law-4",
}
SOURCE = "verified_formal_roster_70_4_20260822"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"backup already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_backup(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    if os.name != "nt":
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("roster backup must have mode 0600")
        if metadata.st_uid != os.geteuid():
            raise RuntimeError("roster backup owner mismatch")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("roster backup schema mismatch")
    if payload.get("migration_key") != MIGRATION_KEY:
        raise RuntimeError("roster backup migration key mismatch")
    expected = str(payload.get("payload_sha256") or "")
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    if not expected or _digest(unsigned) != expected:
        raise RuntimeError("roster backup checksum mismatch")
    return payload


async def _global_counts(session: Any, *, on_date: date) -> dict[str, int]:
    row = (
        await session.execute(
            text(
                """
                SELECT
                    count(*) AS total_count,
                    count(*) FILTER (WHERE teams.active IS TRUE) AS child_count,
                    count(*) FILTER (
                        WHERE teams.active IS FALSE AND teams.code = :center_code
                    ) AS center_count,
                    count(*) FILTER (
                        WHERE users.name IN (:first_name, :second_name)
                          AND teams.active IS FALSE
                          AND teams.code = :center_code
                    ) AS target_center_count
                FROM legal_daily_team_memberships memberships
                JOIN users ON users.id = memberships.user_id
                JOIN teams ON teams.id = memberships.team_id
                WHERE memberships.effective_from <= :on_date
                  AND (
                      memberships.effective_to IS NULL
                      OR memberships.effective_to >= :on_date
                  )
                  AND users.active IS TRUE
                  AND teams.department_name = :parent_department
                  AND (teams.active IS TRUE OR teams.code = :center_code)
                """
            ),
            {
                "on_date": on_date,
                "center_code": CENTER_TEAM_CODE,
                "parent_department": PARENT_DEPARTMENT,
                "first_name": tuple(TARGET_TEAMS)[0],
                "second_name": tuple(TARGET_TEAMS)[1],
            },
        )
    ).mappings().one()
    return {key: int(row[key]) for key in row.keys()}


async def _target_rows(session: Any, *, lock: bool) -> list[dict[str, Any]]:
    suffix = " FOR UPDATE OF memberships, users, teams" if lock else ""
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT
                        memberships.membership_id::text AS membership_id,
                        memberships.tenant_id,
                        memberships.user_id::text AS user_id,
                        memberships.team_id::text AS team_id,
                        memberships.member_role,
                        memberships.effective_from,
                        memberships.effective_to,
                        memberships.source,
                        memberships.data_complete,
                        users.name AS user_name,
                        users.team_id::text AS user_team_id,
                        teams.code AS team_code,
                        teams.name AS team_name
                    FROM legal_daily_team_memberships memberships
                    JOIN users ON users.id = memberships.user_id
                    JOIN teams ON teams.id = memberships.team_id
                    WHERE users.name IN (:first_name, :second_name)
                      AND memberships.effective_from <= :effective_date
                      AND (
                          memberships.effective_to IS NULL
                          OR memberships.effective_to >= :effective_date
                      )
                    ORDER BY users.name, memberships.membership_id
                    """
                    + suffix
                ),
                {
                    "effective_date": EFFECTIVE_DATE,
                    "first_name": tuple(TARGET_TEAMS)[0],
                    "second_name": tuple(TARGET_TEAMS)[1],
                },
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            key: (
                value.isoformat()
                if isinstance(value, (date, datetime))
                else bool(value)
                if isinstance(value, bool)
                else str(value) if value is not None else None
            )
            for key, value in row.items()
        }
        for row in rows
    ]


async def _center_team(session: Any, *, lock: bool) -> dict[str, Any]:
    suffix = " FOR UPDATE" if lock else ""
    row = (
        await session.execute(
            text(
                """
                SELECT id::text AS team_id, code, name, department_name, active
                FROM teams
                WHERE code = :center_code
                """
                + suffix
            ),
            {"center_code": CENTER_TEAM_CODE},
        )
    ).mappings().one()
    return {
        "team_id": str(row["team_id"]),
        "code": str(row["code"]),
        "name": str(row["name"]),
        "department_name": str(row["department_name"]),
        "active": bool(row["active"]),
    }


def _json_safe_row(row: Any) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, (date, datetime)) else value
        for key, value in row.items()
    }


async def _table_exists(session: Any, table_name: str) -> bool:
    return bool(
        await session.scalar(
            text("SELECT to_regclass(:table_name) IS NOT NULL"),
            {"table_name": f"public.{table_name}"},
        )
    )


async def _snapshot_query(
    session: Any,
    *,
    sql: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    rows = [
        _json_safe_row(row)
        for row in (await session.execute(text(sql), parameters)).mappings().all()
    ]
    return {"count": len(rows), "sha256": _digest({"rows": rows})}


async def _dependency_snapshot(
    session: Any,
    *,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    user_ids = [str(row["user_id"]) for row in rows]
    if len(user_ids) != 2:
        raise RuntimeError("dependency snapshot requires exactly two target users")
    parameters = {
        "first_user_id": user_ids[0],
        "second_user_id": user_ids[1],
        "effective_date": EFFECTIVE_DATE,
    }
    snapshots: dict[str, Any] = {
        "daily_reports": await _snapshot_query(
            session,
            sql="""
                SELECT id::text AS id, user_id::text AS user_id,
                       team_id::text AS team_id, date
                FROM daily_reports
                WHERE user_id IN (
                    CAST(:first_user_id AS uuid), CAST(:second_user_id AS uuid)
                )
                  AND date >= :effective_date
                ORDER BY id
            """,
            parameters=parameters,
        ),
        "access_assignments": await _snapshot_query(
            session,
            sql="""
                SELECT assignment_id::text AS assignment_id,
                       principal_user_id, dashboard_role,
                       team_id::text AS team_id, effective_from,
                       effective_to, active, source
                FROM legal_daily_access_assignments
                WHERE principal_user_id IN (:first_user_id, :second_user_id)
                ORDER BY assignment_id
            """,
            parameters=parameters,
        ),
    }
    if await _table_exists(session, "agent2_weekly_plan_roster_members"):
        snapshots["weekly_plan_roster_members"] = await _snapshot_query(
            session,
            sql="""
                SELECT roster_member_id::text AS roster_member_id,
                       user_id, team_id, team_name, created_at
                FROM agent2_weekly_plan_roster_members
                WHERE user_id IN (:first_user_id, :second_user_id)
                ORDER BY roster_member_id
            """,
            parameters=parameters,
        )
    else:
        snapshots["weekly_plan_roster_members"] = {
            "count": 0,
            "sha256": _digest({"rows": []}),
        }
    if await _table_exists(session, "agent2_personal_weekly_briefs"):
        snapshots["personal_weekly_briefs"] = await _snapshot_query(
            session,
            sql="""
                SELECT brief_id::text AS brief_id, owner_user_id,
                       week_start, status, created_at
                FROM agent2_personal_weekly_briefs
                WHERE owner_user_id IN (:first_user_id, :second_user_id)
                ORDER BY brief_id
            """,
            parameters=parameters,
        )
    else:
        snapshots["personal_weekly_briefs"] = {
            "count": 0,
            "sha256": _digest({"rows": []}),
        }
    return snapshots


async def _obligation_rows(
    session: Any,
    *,
    rows: list[dict[str, Any]],
    lock: bool,
) -> list[dict[str, Any]]:
    user_ids = [str(row["user_id"]) for row in rows]
    suffix = " FOR UPDATE" if lock else ""
    obligations = (
        (
            await session.execute(
                text(
                    """
                    SELECT obligation_id::text AS obligation_id, tenant_id,
                           user_id::text AS user_id, team_id::text AS team_id,
                           report_date, required, exemption_reason,
                           deadline_at, source, data_complete
                    FROM legal_daily_submission_obligations
                    WHERE user_id IN (
                        CAST(:first_user_id AS uuid),
                        CAST(:second_user_id AS uuid)
                    )
                      AND report_date >= :effective_date
                    ORDER BY obligation_id
                    """
                    + suffix
                ),
                {
                    "first_user_id": user_ids[0],
                    "second_user_id": user_ids[1],
                    "effective_date": EFFECTIVE_DATE,
                },
            )
        )
        .mappings()
        .all()
    )
    return [_json_safe_row(row) for row in obligations]


def _expected_after_obligations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    center_team_id = payload["center_team"]["team_id"]
    return [
        {**dict(row), "team_id": center_team_id}
        for row in payload.get("obligations", [])
    ]


def _validate_before(
    *,
    counts: dict[str, int],
    rows: list[dict[str, Any]],
    center_team: dict[str, Any],
) -> None:
    if counts != {
        "total_count": 74,
        "child_count": 72,
        "center_count": 2,
        "target_center_count": 0,
    }:
        raise RuntimeError(f"unexpected pre-migration roster counts: {counts}")
    if len(rows) != 2 or {row["user_name"] for row in rows} != set(TARGET_TEAMS):
        raise RuntimeError("target users do not each have exactly one effective membership")
    tenant_ids = {row["tenant_id"] for row in rows}
    if len(tenant_ids) != 1:
        raise RuntimeError("target users do not share one tenant")
    for row in rows:
        if row["team_code"] != TARGET_TEAMS[row["user_name"]]:
            raise RuntimeError("target user has an unexpected source team")
        if row["user_team_id"] != row["team_id"]:
            raise RuntimeError("target user and membership team differ before migration")
        if not row["data_complete"]:
            raise RuntimeError("target membership is incomplete")
    if (
        center_team["department_name"] != PARENT_DEPARTMENT
        or center_team["active"] is not False
    ):
        raise RuntimeError("center team contract changed")


async def backup(path: Path) -> None:
    async with AsyncSessionLocal() as session:
        counts = await _global_counts(session, on_date=EFFECTIVE_DATE)
        rows = await _target_rows(session, lock=False)
        center_team = await _center_team(session, lock=False)
        dependencies = await _dependency_snapshot(session, rows=rows)
        obligations = await _obligation_rows(session, rows=rows, lock=False)
    _validate_before(counts=counts, rows=rows, center_team=center_team)
    unsigned: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "migration_key": MIGRATION_KEY,
        "effective_date": EFFECTIVE_DATE.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "before_counts": counts,
        "center_team": center_team,
        "memberships": rows,
        "dependencies": dependencies,
        "obligations": obligations,
        "new_membership_ids": {
            row["user_name"]: str(uuid4()) for row in rows
        },
    }
    payload = dict(unsigned)
    payload["payload_sha256"] = _digest(unsigned)
    _write_private_json(path, payload)
    print(json.dumps({"action": "backup", "target_count": len(rows), "mode": "pre_migration"}))


def _expected_before_from_backup(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in payload["memberships"]]


async def apply(path: Path) -> None:
    payload = _load_backup(path)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await _apply_in_transaction(session, payload)
            await _verify_in_session(session, expected="after", payload=payload)
    await verify(expected="after")
    print(json.dumps({"action": "apply", "target_count": 2, "effective_date": EFFECTIVE_DATE.isoformat()}))


async def _apply_in_transaction(session: Any, payload: dict[str, Any]) -> None:
    counts = await _global_counts(session, on_date=EFFECTIVE_DATE)
    rows = await _target_rows(session, lock=True)
    center_team = await _center_team(session, lock=True)
    _validate_before(counts=counts, rows=rows, center_team=center_team)
    if rows != _expected_before_from_backup(payload) or center_team != payload["center_team"]:
        raise RuntimeError("production roster changed after backup; refusing apply")
    if await _dependency_snapshot(session, rows=rows) != payload.get("dependencies"):
        raise RuntimeError("related production records changed after backup; refusing apply")
    if await _obligation_rows(session, rows=rows, lock=True) != payload.get("obligations"):
        raise RuntimeError("submission obligations changed after backup; refusing apply")
    for row in rows:
        closed = await session.execute(
            text(
                """
                UPDATE legal_daily_team_memberships
                SET effective_to = :previous_date,
                    updated_at = now()
                WHERE membership_id = CAST(:membership_id AS uuid)
                  AND (effective_to IS NULL OR effective_to >= :effective_date)
                """
            ),
            {
                "membership_id": row["membership_id"],
                "effective_date": EFFECTIVE_DATE,
                "previous_date": EFFECTIVE_DATE - timedelta(days=1),
            },
        )
        if closed.rowcount != 1:
            raise RuntimeError("original membership changed during apply")
        await session.execute(
            text(
                """
                INSERT INTO legal_daily_team_memberships (
                    membership_id, tenant_id, user_id, team_id,
                    member_role, effective_from, effective_to,
                    source, data_complete, created_at, updated_at
                ) VALUES (
                    CAST(:membership_id AS uuid), :tenant_id,
                    CAST(:user_id AS uuid), CAST(:team_id AS uuid),
                    :member_role, :effective_date, NULL,
                    :source, TRUE, now(), now()
                )
                """
            ),
            {
                "membership_id": payload["new_membership_ids"][row["user_name"]],
                "tenant_id": row["tenant_id"],
                "user_id": row["user_id"],
                "team_id": center_team["team_id"],
                "member_role": row["member_role"],
                "effective_date": EFFECTIVE_DATE,
                "source": SOURCE,
            },
        )
        moved = await session.execute(
            text(
                """
                UPDATE users
                SET team_id = CAST(:team_id AS uuid), updated_at = now()
                WHERE id = CAST(:user_id AS uuid)
                  AND team_id = CAST(:old_team_id AS uuid)
                """
            ),
            {
                "team_id": center_team["team_id"],
                "user_id": row["user_id"],
                "old_team_id": row["team_id"],
            },
        )
        if moved.rowcount != 1:
            raise RuntimeError("user team changed during apply")
    for obligation in payload.get("obligations", []):
        moved_obligation = await session.execute(
            text(
                """
                UPDATE legal_daily_submission_obligations
                SET team_id = CAST(:center_team_id AS uuid), updated_at = now()
                WHERE obligation_id = CAST(:obligation_id AS uuid)
                  AND user_id = CAST(:user_id AS uuid)
                  AND team_id = CAST(:old_team_id AS uuid)
                  AND report_date >= :effective_date
                """
            ),
            {
                "center_team_id": center_team["team_id"],
                "obligation_id": obligation["obligation_id"],
                "user_id": obligation["user_id"],
                "old_team_id": obligation["team_id"],
                "effective_date": EFFECTIVE_DATE,
            },
        )
        if moved_obligation.rowcount != 1:
            raise RuntimeError("submission obligation changed during apply")


async def _after_rows(session: Any, *, lock: bool = False) -> list[dict[str, Any]]:
    suffix = " FOR UPDATE OF memberships, users, teams" if lock else ""
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT
                        memberships.membership_id::text AS membership_id,
                        memberships.tenant_id,
                        memberships.user_id::text AS user_id,
                        memberships.team_id::text AS team_id,
                        memberships.member_role,
                        memberships.effective_from,
                        memberships.effective_to,
                        memberships.source,
                        memberships.data_complete,
                        users.name AS user_name,
                        users.team_id::text AS user_team_id,
                        teams.code AS team_code,
                        teams.name AS team_name
                    FROM legal_daily_team_memberships memberships
                    JOIN users ON users.id = memberships.user_id
                    JOIN teams ON teams.id = memberships.team_id
                    WHERE users.name IN (:first_name, :second_name)
                      AND memberships.effective_from <= :effective_date
                      AND (
                          memberships.effective_to IS NULL
                          OR memberships.effective_to >= :effective_date
                      )
                    ORDER BY users.name, memberships.membership_id
                    """
                    + suffix
                ),
                {
                    "effective_date": EFFECTIVE_DATE,
                    "first_name": tuple(TARGET_TEAMS)[0],
                    "second_name": tuple(TARGET_TEAMS)[1],
                },
            )
        )
        .mappings()
        .all()
    )
    return [_json_safe_row(row) for row in rows]


def _expected_after_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    center_team = payload["center_team"]
    expected = []
    for row in _expected_before_from_backup(payload):
        expected.append(
            {
                "membership_id": payload["new_membership_ids"][row["user_name"]],
                "tenant_id": row["tenant_id"],
                "user_id": row["user_id"],
                "team_id": center_team["team_id"],
                "member_role": row["member_role"],
                "effective_from": EFFECTIVE_DATE.isoformat(),
                "effective_to": None,
                "source": SOURCE,
                "data_complete": True,
                "user_name": row["user_name"],
                "user_team_id": center_team["team_id"],
                "team_code": center_team["code"],
                "team_name": center_team["name"],
            }
        )
    return sorted(expected, key=lambda item: (item["user_name"], item["membership_id"]))


async def _closed_original_rows(
    session: Any,
    *,
    payload: dict[str, Any],
    lock: bool,
) -> list[dict[str, Any]]:
    backup_rows = _expected_before_from_backup(payload)
    suffix = " FOR UPDATE" if lock else ""
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT membership_id::text AS membership_id, tenant_id,
                           user_id::text AS user_id, team_id::text AS team_id,
                           member_role, effective_from, effective_to,
                           source, data_complete
                    FROM legal_daily_team_memberships
                    WHERE membership_id IN (
                        CAST(:first_membership_id AS uuid),
                        CAST(:second_membership_id AS uuid)
                    )
                    ORDER BY membership_id
                    """
                    + suffix
                ),
                {
                    "first_membership_id": backup_rows[0]["membership_id"],
                    "second_membership_id": backup_rows[1]["membership_id"],
                },
            )
        )
        .mappings()
        .all()
    )
    return [_json_safe_row(row) for row in rows]


def _expected_closed_original_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    expected = []
    for row in _expected_before_from_backup(payload):
        expected.append(
            {
                "membership_id": row["membership_id"],
                "tenant_id": row["tenant_id"],
                "user_id": row["user_id"],
                "team_id": row["team_id"],
                "member_role": row["member_role"],
                "effective_from": row["effective_from"],
                "effective_to": (EFFECTIVE_DATE - timedelta(days=1)).isoformat(),
                "source": row["source"],
                "data_complete": row["data_complete"],
            }
        )
    return sorted(expected, key=lambda item: item["membership_id"])


async def verify(*, expected: str) -> None:
    async with AsyncSessionLocal() as session:
        counts = await _verify_in_session(session, expected=expected)
    print(json.dumps({"action": "verify", "expected": expected, **counts}))


async def _verify_in_session(
    session: Any,
    *,
    expected: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, int]:
    counts = await _global_counts(session, on_date=EFFECTIVE_DATE)
    if expected == "before":
        rows = await _target_rows(session, lock=False)
        center_team = await _center_team(session, lock=False)
        _validate_before(counts=counts, rows=rows, center_team=center_team)
        return counts
    rows = await _after_rows(session)
    if counts != {
        "total_count": 74,
        "child_count": 70,
        "center_count": 4,
        "target_center_count": 2,
    }:
        raise RuntimeError(f"unexpected post-migration roster counts: {counts}")
    if len(rows) != 2:
        raise RuntimeError("target users do not each have one post-migration membership")
    if payload is not None:
        if rows != _expected_after_rows(payload):
            raise RuntimeError("post-migration membership snapshot is not exact")
        closed = await _closed_original_rows(session, payload=payload, lock=False)
        if closed != _expected_closed_original_rows(payload):
            raise RuntimeError("original membership history is not exact")
        if await _obligation_rows(
            session,
            rows=_expected_before_from_backup(payload),
            lock=False,
        ) != _expected_after_obligations(payload):
            raise RuntimeError("post-migration submission obligations are not exact")
    for row in rows:
        if (
            row["team_code"] != CENTER_TEAM_CODE
            or str(row["user_team_id"]) != str(row["team_id"])
            or row["source"] != SOURCE
            or not bool(row["data_complete"])
        ):
            raise RuntimeError("post-migration target state is invalid")
    return counts


async def smoke(path: Path) -> None:
    payload = _load_backup(path)
    async with AsyncSessionLocal() as session:
        transaction = await session.begin()
        try:
            await _apply_in_transaction(session, payload)
            counts = await _verify_in_session(session, expected="after", payload=payload)
        finally:
            await transaction.rollback()
    await verify(expected="before")
    print(json.dumps({"action": "smoke", "rolled_back": True, **counts}))


async def restore(path: Path) -> None:
    payload = _load_backup(path)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            counts = await _global_counts(session, on_date=EFFECTIVE_DATE)
            if counts != {
                "total_count": 74,
                "child_count": 70,
                "center_count": 4,
                "target_center_count": 2,
            }:
                raise RuntimeError("post-migration roster changed; refusing restore")
            backup_rows = _expected_before_from_backup(payload)
            if await _dependency_snapshot(session, rows=backup_rows) != payload.get("dependencies"):
                raise RuntimeError("related production records changed; refusing restore")
            if await _obligation_rows(
                session,
                rows=backup_rows,
                lock=True,
            ) != _expected_after_obligations(payload):
                raise RuntimeError("submission obligations changed; refusing restore")
            center_team = await _center_team(session, lock=True)
            if await _after_rows(session, lock=True) != _expected_after_rows(payload):
                raise RuntimeError("new memberships changed; refusing restore")
            if await _closed_original_rows(session, payload=payload, lock=True) != _expected_closed_original_rows(payload):
                raise RuntimeError("original membership history changed; refusing restore")
            for row in backup_rows:
                new_membership_id = payload["new_membership_ids"][row["user_name"]]
                deleted = await session.execute(
                    text(
                        """
                        DELETE FROM legal_daily_team_memberships
                        WHERE membership_id = CAST(:membership_id AS uuid)
                          AND user_id = CAST(:user_id AS uuid)
                          AND team_id = CAST(:team_id AS uuid)
                          AND effective_from = :effective_date
                          AND effective_to IS NULL
                          AND source = :source
                        """
                    ),
                    {
                        "membership_id": new_membership_id,
                        "user_id": row["user_id"],
                        "team_id": center_team["team_id"],
                        "effective_date": EFFECTIVE_DATE,
                        "source": SOURCE,
                    },
                )
                if deleted.rowcount != 1:
                    raise RuntimeError("new membership changed; refusing restore")
                restored = await session.execute(
                    text(
                        """
                        UPDATE legal_daily_team_memberships
                        SET effective_to = :effective_to, updated_at = now()
                        WHERE membership_id = CAST(:membership_id AS uuid)
                          AND effective_to = :previous_date
                        """
                    ),
                    {
                        "membership_id": row["membership_id"],
                        "effective_to": (
                            date.fromisoformat(row["effective_to"])
                            if row["effective_to"]
                            else None
                        ),
                        "previous_date": EFFECTIVE_DATE - timedelta(days=1),
                    },
                )
                if restored.rowcount != 1:
                    raise RuntimeError("original membership changed; refusing restore")
                user_restored = await session.execute(
                    text(
                        """
                        UPDATE users
                        SET team_id = CAST(:old_team_id AS uuid), updated_at = now()
                        WHERE id = CAST(:user_id AS uuid)
                          AND team_id = CAST(:center_team_id AS uuid)
                        """
                    ),
                    {
                        "old_team_id": row["team_id"],
                        "user_id": row["user_id"],
                        "center_team_id": center_team["team_id"],
                    },
                )
                if user_restored.rowcount != 1:
                    raise RuntimeError("user team changed; refusing restore")
            for obligation in payload.get("obligations", []):
                restored_obligation = await session.execute(
                    text(
                        """
                        UPDATE legal_daily_submission_obligations
                        SET team_id = CAST(:old_team_id AS uuid), updated_at = now()
                        WHERE obligation_id = CAST(:obligation_id AS uuid)
                          AND user_id = CAST(:user_id AS uuid)
                          AND team_id = CAST(:center_team_id AS uuid)
                          AND report_date >= :effective_date
                        """
                    ),
                    {
                        "old_team_id": obligation["team_id"],
                        "obligation_id": obligation["obligation_id"],
                        "user_id": obligation["user_id"],
                        "center_team_id": center_team["team_id"],
                        "effective_date": EFFECTIVE_DATE,
                    },
                )
                if restored_obligation.rowcount != 1:
                    raise RuntimeError("submission obligation changed during restore")
            if await _obligation_rows(
                session,
                rows=backup_rows,
                lock=False,
            ) != payload.get("obligations"):
                raise RuntimeError("submission obligations were not restored exactly")
            await _verify_in_session(session, expected="before")
    await verify(expected="before")
    print(json.dumps({"action": "restore", "target_count": 2}))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("backup", "smoke", "apply", "verify-before", "verify-after", "restore"),
    )
    parser.add_argument("--backup-path", type=Path)
    args = parser.parse_args()
    if args.action in {"backup", "smoke", "apply", "restore"} and args.backup_path is None:
        parser.error("--backup-path is required")
    try:
        if args.action == "backup":
            await backup(args.backup_path)
        elif args.action == "smoke":
            await smoke(args.backup_path)
        elif args.action == "apply":
            await apply(args.backup_path)
        elif args.action == "restore":
            await restore(args.backup_path)
        elif args.action == "verify-before":
            await verify(expected="before")
        else:
            await verify(expected="after")
    except Exception as exc:
        error_text = str(exc)
        print(
            json.dumps(
                {
                    "action": args.action,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error_sha256": hashlib.sha256(error_text.encode("utf-8")).hexdigest(),
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
