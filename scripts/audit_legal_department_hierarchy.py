from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from sqlalchemy import text

from app.db import AsyncSessionLocal


PARENT = "法务合约中心"
CANONICAL_ID = "783ac5ac-527a-40b0-9a3c-fb53d8d4f951"
DUPLICATE_ID = "6825dcda-6c35-40d9-83d3-6af93e4a5d47"
TABLES = (
    "legal_daily_review_suggestions",
    "legal_daily_submission_obligations",
    "legal_daily_team_memberships",
    "legal_daily_work_items",
    "team_summaries",
)
SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote(value: str) -> str:
    if not SAFE.fullmatch(value):
        raise RuntimeError(f"unsafe identifier: {value}")
    return f'"{value}"'


def clean(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    return str(value)


async def main() -> None:
    async with AsyncSessionLocal() as session:
        team_rows = (
            await session.execute(
                text(
                    """
                    SELECT t.id::text, t.code, t.name, t.department_name, t.active,
                           t.created_at::text, t.updated_at::text,
                           (SELECT count(*) FROM users u WHERE u.team_id = t.id) AS users,
                           (SELECT count(*) FROM daily_reports d WHERE d.team_id = t.id) AS daily_reports,
                           (SELECT count(*) FROM legal_daily_team_memberships m WHERE m.team_id = t.id) AS dashboard_memberships
                    FROM teams t
                    WHERE t.department_name = :parent
                    ORDER BY t.name, t.created_at, t.code
                    """
                ),
                {"parent": PARENT},
            )
        ).mappings().all()

        table_details: list[dict[str, Any]] = []
        for table in TABLES:
            qtable = quote(table)
            columns = (
                await session.execute(
                    text(
                        """
                        SELECT column_name, data_type, udt_name, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = :table
                        ORDER BY ordinal_position
                        """
                    ),
                    {"table": table},
                )
            ).mappings().all()
            constraints = (
                await session.execute(
                    text(
                        """
                        SELECT conname, contype::text, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE conrelid = CAST(('public.' || :table) AS regclass)
                        ORDER BY conname
                        """
                    ),
                    {"table": table},
                )
            ).mappings().all()
            indexes = (
                await session.execute(
                    text(
                        """
                        SELECT indexname, indexdef
                        FROM pg_indexes
                        WHERE schemaname = 'public' AND tablename = :table
                        ORDER BY indexname
                        """
                    ),
                    {"table": table},
                )
            ).mappings().all()
            counts = (
                await session.execute(
                    text(
                        f"SELECT team_id::text AS team_id, count(*) AS row_count "
                        f"FROM {qtable} "
                        "WHERE team_id::text IN (:canonical, :duplicate) "
                        "GROUP BY team_id ORDER BY team_id"
                    ),
                    {"canonical": CANONICAL_ID, "duplicate": DUPLICATE_ID},
                )
            ).mappings().all()
            table_details.append(
                {
                    "table": table,
                    "columns": [dict(row) for row in columns],
                    "constraints": [dict(row) for row in constraints],
                    "indexes": [dict(row) for row in indexes],
                    "counts": [dict(row) for row in counts],
                }
            )

        print(
            json.dumps(
                {
                    "parent": PARENT,
                    "teams": [dict(row) for row in team_rows],
                    "tables": table_details,
                },
                ensure_ascii=False,
                default=clean,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
