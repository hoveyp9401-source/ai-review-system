from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db import AsyncSessionLocal


CANONICAL_ID = "783ac5ac-527a-40b0-9a3c-fb53d8d4f951"
DUPLICATE_ID = "6825dcda-6c35-40d9-83d3-6af93e4a5d47"
CANONICAL_CODE = "team-01"
DUPLICATE_CODE = "monthly-admin"
TARGET_NAME = "综合管理部"
TARGET_DEPARTMENT = "法务合约中心"
BACKUP_DIR = Path(
    "/home/ai_review_tunnel/codex_backups/"
    "report_insights_20260806_before_team_dedup"
)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_identifier(value: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
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
                SELECT table_schema, table_name, column_name, data_type, udt_name
                FROM information_schema.columns
                WHERE column_name = 'team_id'
                  AND table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name
                """
            )
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def duplicate_reference_counts(session, columns: list[dict[str, str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for column in columns:
        schema = str(column["table_schema"])
        table = str(column["table_name"])
        name = str(column["column_name"])
        qualified = f"{safe_identifier(schema)}.{safe_identifier(table)}"
        quoted_column = safe_identifier(name)
        count = (
            await session.execute(
                text(
                    f"SELECT count(*) FROM {qualified} "
                    f"WHERE {quoted_column}::text = :duplicate_id"
                ),
                {"duplicate_id": DUPLICATE_ID},
            )
        ).scalar_one()
        results.append({**column, "duplicate_rows": int(count)})
    return results


async def locked_teams(session) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT *
                FROM teams
                WHERE id::text IN (:canonical_id, :duplicate_id)
                ORDER BY code
                FOR UPDATE
                """
            ),
            {"canonical_id": CANONICAL_ID, "duplicate_id": DUPLICATE_ID},
        )
    ).mappings().all()
    return [{str(key): json_value(value) for key, value in row.items()} for row in rows]


def validate_teams(teams: list[dict[str, Any]]) -> None:
    if len(teams) != 2:
        raise RuntimeError(f"expected two exact team rows, found {len(teams)}")
    by_id = {str(team["id"]): team for team in teams}
    if set(by_id) != {CANONICAL_ID, DUPLICATE_ID}:
        raise RuntimeError(f"unexpected team IDs: {sorted(by_id)}")
    canonical = by_id[CANONICAL_ID]
    duplicate = by_id[DUPLICATE_ID]
    expected = (
        (canonical, CANONICAL_CODE),
        (duplicate, DUPLICATE_CODE),
    )
    for row, code in expected:
        actual = (
            str(row.get("code") or ""),
            str(row.get("name") or ""),
            str(row.get("department_name") or ""),
            bool(row.get("active")),
        )
        wanted = (code, TARGET_NAME, TARGET_DEPARTMENT, True)
        if actual != wanted:
            raise RuntimeError(f"team identity changed for {row.get('id')}: {actual!r}")


async def summary_rows(session) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT *
                FROM team_summaries
                WHERE team_id::text IN (:canonical_id, :duplicate_id)
                ORDER BY team_id, date, id
                FOR UPDATE
                """
            ),
            {"canonical_id": CANONICAL_ID, "duplicate_id": DUPLICATE_ID},
        )
    ).mappings().all()
    return [{str(key): json_value(value) for key, value in row.items()} for row in rows]


def write_backup(teams: list[dict[str, Any]], summaries: list[dict[str, Any]], references: list[dict[str, Any]]) -> tuple[Path, str]:
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=False)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "before merging duplicate 综合管理部 into canonical team",
        "canonical_id": CANONICAL_ID,
        "duplicate_id": DUPLICATE_ID,
        "teams": teams,
        "team_summaries": summaries,
        "team_id_reference_counts": references,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    target = BACKUP_DIR / "affected_rows.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return target, digest


async def inspect() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        columns = await team_id_columns(session)
        references = await duplicate_reference_counts(session, columns)
        teams = await locked_teams(session)
        validate_teams(teams)
        summaries = await summary_rows(session)
        by_team: dict[str, int] = {}
        for summary in summaries:
            team_id = str(summary.get("team_id"))
            by_team[team_id] = by_team.get(team_id, 0) + 1
        return {
            "mode": "check",
            "teams": [
                {
                    "id": team["id"],
                    "code": team["code"],
                    "name": team["name"],
                    "department_name": team["department_name"],
                    "active": team["active"],
                }
                for team in teams
            ],
            "summary_counts": by_team,
            "team_id_references": references,
        }


async def apply() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            teams = await locked_teams(session)
            validate_teams(teams)
            columns = await team_id_columns(session)
            references_before = await duplicate_reference_counts(session, columns)
            unexpected = [
                row
                for row in references_before
                if row["duplicate_rows"]
                and not (
                    row["table_schema"] == "public"
                    and row["table_name"] == "team_summaries"
                    and row["column_name"] == "team_id"
                )
            ]
            if unexpected:
                raise RuntimeError(f"unexpected duplicate team references: {unexpected!r}")
            duplicate_summary_count = sum(
                row["duplicate_rows"]
                for row in references_before
                if row["table_schema"] == "public"
                and row["table_name"] == "team_summaries"
                and row["column_name"] == "team_id"
            )
            if duplicate_summary_count != 18:
                raise RuntimeError(
                    f"expected 18 duplicate summaries, found {duplicate_summary_count}"
                )
            summaries = await summary_rows(session)
            backup_path, backup_sha256 = write_backup(teams, summaries, references_before)

            updated = (
                await session.execute(
                    text(
                        """
                        UPDATE team_summaries
                        SET team_id = :canonical_id, updated_at = now()
                        WHERE team_id::text = :duplicate_id
                        """
                    ),
                    {"canonical_id": CANONICAL_ID, "duplicate_id": DUPLICATE_ID},
                )
            ).rowcount
            if updated != 18:
                raise RuntimeError(f"expected to update 18 summaries, updated {updated}")

            deleted = (
                await session.execute(
                    text(
                        "DELETE FROM teams WHERE id::text = :duplicate_id "
                        "AND code = :duplicate_code"
                    ),
                    {"duplicate_id": DUPLICATE_ID, "duplicate_code": DUPLICATE_CODE},
                )
            ).rowcount
            if deleted != 1:
                raise RuntimeError(f"expected to delete one duplicate team, deleted {deleted}")

            references_after = await duplicate_reference_counts(session, columns)
            remaining_references = [row for row in references_after if row["duplicate_rows"]]
            if remaining_references:
                raise RuntimeError(f"duplicate references remain: {remaining_references!r}")
            exact_name_count = (
                await session.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM teams
                        WHERE name = :name
                          AND department_name = :department
                          AND active IS TRUE
                        """
                    ),
                    {"name": TARGET_NAME, "department": TARGET_DEPARTMENT},
                )
            ).scalar_one()
            if int(exact_name_count) != 1:
                raise RuntimeError(
                    f"expected exactly one active matching team, found {exact_name_count}"
                )
            canonical_summary_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM team_summaries "
                        "WHERE team_id::text = :canonical_id"
                    ),
                    {"canonical_id": CANONICAL_ID},
                )
            ).scalar_one()
            if int(canonical_summary_count) != 25:
                raise RuntimeError(
                    f"expected 25 canonical summaries, found {canonical_summary_count}"
                )

        return {
            "mode": "apply",
            "status": "committed",
            "canonical_id": CANONICAL_ID,
            "deleted_duplicate_id": DUPLICATE_ID,
            "migrated_team_summaries": 18,
            "canonical_team_summaries": 25,
            "active_matching_teams": 1,
            "backup_path": str(backup_path),
            "backup_sha256": backup_sha256,
        }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("check", "apply"))
    args = parser.parse_args()
    result = await (inspect() if args.mode == "check" else apply())
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
