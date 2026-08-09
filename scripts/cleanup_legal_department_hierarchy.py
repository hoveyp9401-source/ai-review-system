from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db import AsyncSessionLocal


PARENT = "法务合约中心"
TENANT_ID = "legal-daily-production-v1"
ROSTER_DATE = date(2026, 8, 6)
CENTER_ID = "142b37a7-02f0-4d62-9c0e-1435292b5207"
CANONICAL_TEAMS = {
    "c754511b-5f51-4098-b0b8-079c20d7f634": ("monthly-law-1", "法务一部"),
    "3731f7c4-2a7c-4e42-926e-a96cc30c10ef": ("monthly-law-2", "法务二部"),
    "05a5c481-64cd-450d-bee0-8bcceb4fe3e4": ("monthly-law-3", "法务三部"),
    "b62cb736-ec6d-4832-8617-1f1e54105b68": ("monthly-law-4", "法务四部"),
    "ef897423-55a4-4924-a3b6-9cd8997f44dc": ("team-05", "法务五部"),
    "59f06bfc-c96c-46c3-9338-43038e40a28a": ("monthly-law-6", "法务六部"),
    "783ac5ac-527a-40b0-9a3c-fb53d8d4f951": ("team-01", "综合管理部"),
}
SOURCE_TEAMS = {
    "6825dcda-6c35-40d9-83d3-6af93e4a5d47": {
        "code": "monthly-admin",
        "name": "综合管理部",
        "target_id": "783ac5ac-527a-40b0-9a3c-fb53d8d4f951",
    },
    "1b944b5c-df83-4a91-841b-1e6c75eff53d": {
        "code": "monthly-law-5",
        "name": "法务五部",
        "target_id": "ef897423-55a4-4924-a3b6-9cd8997f44dc",
    },
    "badf2859-3753-40e9-a5c0-fe3bcd52abd1": {
        "code": "monthly-zhu-jiajia",
        "name": "朱佳佳",
        "target_id": CENTER_ID,
    },
}
EXPECTED_SOURCE_REFERENCES = {
    "6825dcda-6c35-40d9-83d3-6af93e4a5d47": {
        "public.legal_daily_review_suggestions": 10,
        "public.legal_daily_submission_obligations": 77,
        "public.legal_daily_team_memberships": 12,
        "public.legal_daily_work_items": 13,
        "public.team_summaries": 19,
    },
    "1b944b5c-df83-4a91-841b-1e6c75eff53d": {
        "public.team_summaries": 19,
    },
    "badf2859-3753-40e9-a5c0-fe3bcd52abd1": {
        "public.daily_reports": 1,
        "public.performance_submissions": 1,
        "public.team_summaries": 19,
    },
}
EXPECTED_SUMMARY_CONFLICTS = {
    "6825dcda-6c35-40d9-83d3-6af93e4a5d47": 5,
    "1b944b5c-df83-4a91-841b-1e6c75eff53d": 5,
    "badf2859-3753-40e9-a5c0-fe3bcd52abd1": 0,
}
DIRECT_UPDATES = {
    "6825dcda-6c35-40d9-83d3-6af93e4a5d47": {
        "legal_daily_review_suggestions": 10,
        "legal_daily_submission_obligations": 77,
        "legal_daily_team_memberships": 12,
        "legal_daily_work_items": 13,
    },
    "badf2859-3753-40e9-a5c0-fe3bcd52abd1": {
        "daily_reports": 1,
        "performance_submissions": 1,
    },
}
BACKUP_ROOT = Path(
    "/home/ai_review_tunnel/codex_backups/"
    "report_insights_20260806_before_legal_department_cleanup"
)
SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXPECTED_CURRENT_ROSTER_COUNTS = {
    "c754511b-5f51-4098-b0b8-079c20d7f634": 14,
    "3731f7c4-2a7c-4e42-926e-a96cc30c10ef": 11,
    "05a5c481-64cd-450d-bee0-8bcceb4fe3e4": 9,
    "b62cb736-ec6d-4832-8617-1f1e54105b68": 11,
    "ef897423-55a4-4924-a3b6-9cd8997f44dc": 8,
    "59f06bfc-c96c-46c3-9338-43038e40a28a": 6,
    "783ac5ac-527a-40b0-9a3c-fb53d8d4f951": 11,
}
EXPECTED_ROSTER_SOURCES = {
    "formal_org_import_20260803",
    "current-20-production-pilot-20260727",
}


def quote(value: str) -> str:
    if not SAFE.fullmatch(value):
        raise RuntimeError(f"unsafe SQL identifier: {value!r}")
    return f'"{value}"'


def json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    return str(value)


async def team_id_columns(session) -> list[dict[str, str]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT table_schema, table_name, column_name
                FROM information_schema.columns
                WHERE column_name = 'team_id'
                  AND table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name
                """
            )
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def source_reference_counts(
    session,
    columns: list[dict[str, str]],
) -> dict[str, dict[str, int]]:
    results: dict[str, dict[str, int]] = {source_id: {} for source_id in SOURCE_TEAMS}
    for column in columns:
        schema = str(column["table_schema"])
        table = str(column["table_name"])
        name = str(column["column_name"])
        qualified = f"{quote(schema)}.{quote(table)}"
        quoted_column = quote(name)
        rows = (
            await session.execute(
                text(
                    f"SELECT {quoted_column}::text AS team_id, count(*) AS row_count "
                    f"FROM {qualified} "
                    f"WHERE {quoted_column}::text = ANY(:source_ids) "
                    f"GROUP BY {quoted_column}"
                ),
                {"source_ids": list(SOURCE_TEAMS)},
            )
        ).mappings().all()
        for row in rows:
            count = int(row["row_count"])
            if count:
                results[str(row["team_id"])][f"{schema}.{table}"] = count
    return results


async def hierarchy_rows(session, *, lock: bool) -> list[dict[str, Any]]:
    suffix = " FOR UPDATE" if lock else ""
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text, code, name, department_name, active,
                       created_at::text, updated_at::text
                FROM teams
                WHERE department_name = :parent
                ORDER BY active DESC, name, code
                """
                + suffix
            ),
            {"parent": PARENT},
        )
    ).mappings().all()
    return [{str(key): json_value(value) for key, value in row.items()} for row in rows]


def validate_before_hierarchy(rows: list[dict[str, Any]]) -> None:
    active = {str(row["id"]): row for row in rows if bool(row["active"])}
    expected_active_ids = set(CANONICAL_TEAMS) | set(SOURCE_TEAMS)
    if set(active) != expected_active_ids:
        raise RuntimeError(
            f"active hierarchy changed; expected {sorted(expected_active_ids)}, "
            f"found {sorted(active)}"
        )
    for team_id, (code, name) in CANONICAL_TEAMS.items():
        row = active[team_id]
        actual = (str(row["code"]), str(row["name"]), str(row["department_name"]))
        if actual != (code, name, PARENT):
            raise RuntimeError(f"canonical team changed for {team_id}: {actual!r}")
    for team_id, expected in SOURCE_TEAMS.items():
        row = active[team_id]
        actual = (str(row["code"]), str(row["name"]), str(row["department_name"]))
        wanted = (expected["code"], expected["name"], PARENT)
        if actual != wanted:
            raise RuntimeError(f"cleanup source changed for {team_id}: {actual!r}")
    center = [row for row in rows if str(row["id"]) == CENTER_ID]
    if len(center) != 1:
        raise RuntimeError(f"expected one center-level holder, found {len(center)}")
    center_row = center[0]
    center_identity = (
        str(center_row["code"]),
        str(center_row["name"]),
        str(center_row["department_name"]),
        bool(center_row["active"]),
    )
    if center_identity != ("legal-center", "法务合约中心（中心层级）", PARENT, False):
        raise RuntimeError(f"center-level holder changed: {center_identity!r}")


async def active_duplicate_names(session) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT department_name, name, count(*) AS active_count,
                       array_agg(code ORDER BY code) AS codes
                FROM teams
                WHERE active IS TRUE
                GROUP BY department_name, name
                HAVING count(*) > 1
                ORDER BY department_name, name
                """
            )
        )
    ).mappings().all()
    return [{str(key): json_value(value) for key, value in row.items()} for row in rows]


def validate_before_duplicates(rows: list[dict[str, Any]]) -> None:
    actual = {
        (str(row["department_name"]), str(row["name"])): tuple(row["codes"])
        for row in rows
    }
    expected = {
        (PARENT, "法务五部"): ("monthly-law-5", "team-05"),
        (PARENT, "综合管理部"): ("monthly-admin", "team-01"),
    }
    if actual != expected:
        raise RuntimeError(f"unexpected active duplicate names: {actual!r}")


async def current_roster_snapshot(session) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT m.membership_id::text, m.team_id::text,
                       m.user_id::text, u.name AS user_name,
                       u.team_id::text AS user_team_id,
                       m.member_role, m.effective_from::text,
                       m.effective_to::text, m.source, m.data_complete
                FROM legal_daily_team_memberships m
                JOIN users u ON u.id = m.user_id
                JOIN teams t ON t.id = m.team_id
                WHERE m.tenant_id = :tenant_id
                  AND m.effective_from <= :on_date
                  AND (m.effective_to IS NULL OR m.effective_to >= :on_date)
                  AND t.active IS TRUE
                  AND t.department_name = :parent
                ORDER BY m.team_id, u.name, m.user_id
                """
            ),
            {"tenant_id": TENANT_ID, "on_date": ROSTER_DATE, "parent": PARENT},
        )
    ).mappings().all()
    return [{str(key): json_value(value) for key, value in row.items()} for row in rows]


def validate_current_roster(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    user_counts: dict[str, int] = {}
    for row in rows:
        team_id = str(row["team_id"])
        user_id = str(row["user_id"])
        counts[team_id] = counts.get(team_id, 0) + 1
        user_counts[user_id] = user_counts.get(user_id, 0) + 1
        if str(row["user_team_id"]) != team_id:
            raise RuntimeError(
                f"roster user {row['user_name']} has a different system team: "
                f"{row['user_team_id']} != {team_id}"
            )
        if not bool(row["data_complete"]):
            raise RuntimeError(f"roster row is incomplete for {row['user_name']}")
        if str(row["source"]) not in EXPECTED_ROSTER_SOURCES:
            raise RuntimeError(
                f"unexpected roster source for {row['user_name']}: {row['source']}"
            )
    if counts != EXPECTED_CURRENT_ROSTER_COUNTS:
        raise RuntimeError(
            f"current roster does not match the imported architecture counts: "
            f"expected {EXPECTED_CURRENT_ROSTER_COUNTS!r}, found {counts!r}"
        )
    duplicates = [user_id for user_id, count in user_counts.items() if count != 1]
    if duplicates:
        raise RuntimeError(f"roster users do not belong to exactly one child team: {duplicates}")
    return {
        "as_of": ROSTER_DATE.isoformat(),
        "member_count": len(rows),
        "team_counts": counts,
        "user_team_mismatches": 0,
        "duplicate_memberships": 0,
    }


async def backup_rows(
    session,
    columns: list[dict[str, str]],
    hierarchy: list[dict[str, Any]],
    references: dict[str, dict[str, int]],
    current_roster: list[dict[str, Any]],
) -> tuple[Path, str]:
    affected: dict[str, list[dict[str, Any]]] = {}
    for column in columns:
        schema = str(column["table_schema"])
        table = str(column["table_name"])
        name = str(column["column_name"])
        qualified = f"{quote(schema)}.{quote(table)}"
        quoted_column = quote(name)
        rows = (
            await session.execute(
                text(
                    f"SELECT * FROM {qualified} "
                    f"WHERE {quoted_column}::text = ANY(:source_ids)"
                ),
                {"source_ids": list(SOURCE_TEAMS)},
            )
        ).mappings().all()
        if rows:
            affected[f"{schema}.{table}"] = [
                {str(key): json_value(value) for key, value in row.items()}
                for row in rows
            ]

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "before normalizing 法务合约中心 to seven active child teams",
        "hierarchy_rows": hierarchy,
        "source_to_target": {
            source_id: str(details["target_id"])
            for source_id, details in SOURCE_TEAMS.items()
        },
        "source_reference_counts": references,
        "current_architecture_roster": current_roster,
        "affected_rows": affected,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir = BACKUP_ROOT / (
        datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-") + str(os.getpid())
    )
    run_dir.mkdir(mode=0o700, exist_ok=False)
    target = run_dir / "affected_rows.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return target, digest


async def merge_summaries(session, source_id: str, target_id: str) -> dict[str, int]:
    source_count = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM team_summaries "
                    "WHERE team_id::text = :source_id"
                ),
                {"source_id": source_id},
            )
        ).scalar_one()
    )
    expected_source_count = EXPECTED_SOURCE_REFERENCES[source_id][
        "public.team_summaries"
    ]
    if source_count != expected_source_count:
        raise RuntimeError(
            f"expected {expected_source_count} summaries for {source_id}, "
            f"found {source_count}"
        )
    conflict_count = int(
        (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM team_summaries source
                    WHERE source.team_id::text = :source_id
                      AND EXISTS (
                          SELECT 1
                          FROM team_summaries target
                          WHERE target.team_id::text = :target_id
                            AND target.scope = source.scope
                            AND target.date = source.date
                      )
                    """
                ),
                {"source_id": source_id, "target_id": target_id},
            )
        ).scalar_one()
    )
    expected_conflicts = EXPECTED_SUMMARY_CONFLICTS[source_id]
    if conflict_count != expected_conflicts:
        raise RuntimeError(
            f"summary conflicts changed for {source_id}: "
            f"expected {expected_conflicts}, found {conflict_count}"
        )
    if conflict_count:
        meaningful_conflicts = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM team_summaries source
                        WHERE source.team_id::text = :source_id
                          AND (source.report_count <> 0 OR source.complete_count <> 0)
                          AND EXISTS (
                              SELECT 1
                              FROM team_summaries target
                              WHERE target.team_id::text = :target_id
                                AND target.scope = source.scope
                                AND target.date = source.date
                          )
                        """
                    ),
                    {"source_id": source_id, "target_id": target_id},
                )
            ).scalar_one()
        )
        if meaningful_conflicts:
            raise RuntimeError(
                f"refusing to discard {meaningful_conflicts} meaningful conflicting summaries"
            )
    deleted = (
        await session.execute(
            text(
                """
                DELETE FROM team_summaries source
                WHERE source.team_id::text = :source_id
                  AND EXISTS (
                      SELECT 1
                      FROM team_summaries target
                      WHERE target.team_id::text = :target_id
                        AND target.scope = source.scope
                        AND target.date = source.date
                  )
                """
            ),
            {"source_id": source_id, "target_id": target_id},
        )
    ).rowcount
    if deleted != conflict_count:
        raise RuntimeError(
            f"expected to remove {conflict_count} conflicting summaries, removed {deleted}"
        )
    updated = (
        await session.execute(
            text(
                "UPDATE team_summaries "
                "SET team_id = CAST(:target_id AS uuid) "
                "WHERE team_id::text = :source_id"
            ),
            {"source_id": source_id, "target_id": target_id},
        )
    ).rowcount
    expected_updated = expected_source_count - conflict_count
    if updated != expected_updated:
        raise RuntimeError(
            f"expected to migrate {expected_updated} summaries, migrated {updated}"
        )
    return {"deleted_conflicts": deleted, "migrated": updated}


async def direct_update(
    session,
    *,
    table: str,
    source_id: str,
    target_id: str,
    expected: int,
) -> int:
    updated = (
        await session.execute(
            text(
                f"UPDATE {quote(table)} "
                "SET team_id = CAST(:target_id AS uuid) "
                "WHERE team_id::text = :source_id"
            ),
            {"source_id": source_id, "target_id": target_id},
        )
    ).rowcount
    if updated != expected:
        raise RuntimeError(
            f"expected to migrate {expected} rows in {table}, migrated {updated}"
        )
    return updated


async def validate_after(
    session,
    columns: list[dict[str, str]],
    roster_before: list[dict[str, Any]],
) -> dict[str, Any]:
    remaining = await source_reference_counts(session, columns)
    nonzero = {key: value for key, value in remaining.items() if value}
    if nonzero:
        raise RuntimeError(f"source team references remain: {nonzero!r}")
    active_rows = (
        await session.execute(
            text(
                """
                SELECT id::text, code, name
                FROM teams
                WHERE department_name = :parent AND active IS TRUE
                ORDER BY name
                """
            ),
            {"parent": PARENT},
        )
    ).mappings().all()
    actual = {
        str(row["id"]): (str(row["code"]), str(row["name"]))
        for row in active_rows
    }
    if actual != CANONICAL_TEAMS:
        raise RuntimeError(f"final active hierarchy is not the required seven: {actual!r}")
    duplicate_names = await active_duplicate_names(session)
    if duplicate_names:
        raise RuntimeError(f"active duplicate team names remain: {duplicate_names!r}")
    center_count = int(
        (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM teams
                    WHERE id::text = :center_id
                      AND code = 'legal-center'
                      AND name = '法务合约中心（中心层级）'
                      AND department_name = :parent
                      AND active IS FALSE
                    """
                ),
                {"center_id": CENTER_ID, "parent": PARENT},
            )
        ).scalar_one()
    )
    if center_count != 1:
        raise RuntimeError("center-level holder is missing or changed")
    roster_after = await current_roster_snapshot(session)
    roster_validation = validate_current_roster(roster_after)
    if roster_after != roster_before:
        raise RuntimeError("current architecture roster changed during hierarchy cleanup")
    return {
        "parent": PARENT,
        "active_child_count": len(active_rows),
        "active_children": [str(row["name"]) for row in active_rows],
        "active_duplicate_names": 0,
        "source_references_remaining": 0,
        "architecture_roster": roster_validation,
    }


async def check() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        hierarchy = await hierarchy_rows(session, lock=False)
        validate_before_hierarchy(hierarchy)
        duplicates = await active_duplicate_names(session)
        validate_before_duplicates(duplicates)
        columns = await team_id_columns(session)
        references = await source_reference_counts(session, columns)
        if references != EXPECTED_SOURCE_REFERENCES:
            raise RuntimeError(
                f"source reference counts changed: expected {EXPECTED_SOURCE_REFERENCES!r}, "
                f"found {references!r}"
            )
        roster = await current_roster_snapshot(session)
        roster_validation = validate_current_roster(roster)
        return {
            "mode": "check",
            "status": "ready",
            "current_active_children": len(CANONICAL_TEAMS) + len(SOURCE_TEAMS),
            "required_active_children": len(CANONICAL_TEAMS),
            "duplicate_names": duplicates,
            "source_reference_counts": references,
            "architecture_roster": roster_validation,
        }


async def apply() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('legal_department_hierarchy_cleanup'))")
            )
            await session.execute(text("LOCK TABLE teams IN SHARE ROW EXCLUSIVE MODE"))
            await session.execute(
                text(
                    "LOCK TABLE team_summaries, daily_reports, performance_submissions, "
                    "legal_daily_review_suggestions, "
                    "legal_daily_submission_obligations, "
                    "legal_daily_team_memberships, legal_daily_work_items "
                    "IN SHARE ROW EXCLUSIVE MODE"
                )
            )
            hierarchy = await hierarchy_rows(session, lock=True)
            validate_before_hierarchy(hierarchy)
            duplicates = await active_duplicate_names(session)
            validate_before_duplicates(duplicates)
            columns = await team_id_columns(session)
            references = await source_reference_counts(session, columns)
            if references != EXPECTED_SOURCE_REFERENCES:
                raise RuntimeError(
                    f"source reference counts changed: expected {EXPECTED_SOURCE_REFERENCES!r}, "
                    f"found {references!r}"
                )
            roster_before = await current_roster_snapshot(session)
            validate_current_roster(roster_before)
            backup_path, backup_sha256 = await backup_rows(
                session, columns, hierarchy, references, roster_before
            )

            direct_results: dict[str, dict[str, int]] = {}
            for source_id, tables in DIRECT_UPDATES.items():
                target_id = str(SOURCE_TEAMS[source_id]["target_id"])
                direct_results[source_id] = {}
                for table, expected in tables.items():
                    direct_results[source_id][table] = await direct_update(
                        session,
                        table=table,
                        source_id=source_id,
                        target_id=target_id,
                        expected=expected,
                    )

            summary_results: dict[str, dict[str, int]] = {}
            for source_id, details in SOURCE_TEAMS.items():
                summary_results[source_id] = await merge_summaries(
                    session, source_id, str(details["target_id"])
                )

            deleted = (
                await session.execute(
                    text(
                        "DELETE FROM teams WHERE id::text = ANY(:source_ids)"
                    ),
                    {"source_ids": list(SOURCE_TEAMS)},
                )
            ).rowcount
            if deleted != len(SOURCE_TEAMS):
                raise RuntimeError(
                    f"expected to remove {len(SOURCE_TEAMS)} invalid teams, removed {deleted}"
                )

            final = await validate_after(session, columns, roster_before)
        return {
            "mode": "apply",
            "status": "committed",
            "backup_path": str(backup_path),
            "backup_sha256": backup_sha256,
            "direct_updates": direct_results,
            "summary_merges": summary_results,
            "removed_invalid_teams": len(SOURCE_TEAMS),
            "final": final,
        }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("check", "apply"))
    args = parser.parse_args()
    result = await (check() if args.mode == "check" else apply())
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
