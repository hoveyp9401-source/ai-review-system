from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.db import AsyncSessionLocal


TARGET_NAME = "综合管理部"


async def main() -> None:
    async with AsyncSessionLocal() as session:
        teams = (
            await session.execute(
                text(
                    """
                    SELECT id::text, code, name, department_name,
                           active, created_at::text, updated_at::text
                    FROM teams
                    WHERE name = :name
                    ORDER BY created_at, id
                    """
                ),
                {"name": TARGET_NAME},
            )
        ).mappings().all()
        team_ids = [row["id"] for row in teams]
        references = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT
                           c.conrelid::regclass::text AS table_name,
                           a.attname AS column_name,
                           c.confdeltype::text AS on_delete,
                           c.confupdtype::text AS on_update
                    FROM pg_constraint c
                    JOIN pg_attribute a
                      ON a.attrelid = c.conrelid
                     AND a.attnum = ANY(c.conkey)
                    WHERE c.contype = 'f'
                      AND c.confrelid = 'teams'::regclass
                    ORDER BY table_name, column_name
                    """
                )
            )
        ).mappings().all()

        counts: list[dict[str, object]] = []
        for reference in references:
            table_name = str(reference["table_name"])
            column_name = str(reference["column_name"])
            if not table_name.replace("_", "").isalnum():
                raise RuntimeError(f"unsafe table name: {table_name}")
            if not column_name.replace("_", "").isalnum():
                raise RuntimeError(f"unsafe column name: {column_name}")
            rows = (
                await session.execute(
                    text(
                        f'SELECT "{column_name}"::text AS team_id, count(*) AS row_count '
                        f'FROM "{table_name}" '
                        f'WHERE "{column_name}"::text = ANY(:team_ids) '
                        f'GROUP BY "{column_name}" ORDER BY "{column_name}"'
                    ),
                    {"team_ids": team_ids},
                )
            ).mappings().all()
            counts.append(
                {
                    "table": table_name,
                    "column": column_name,
                    "on_delete": reference["on_delete"],
                    "on_update": reference["on_update"],
                    "counts": [dict(row) for row in rows],
                }
            )

        report_ranges = (
            await session.execute(
                text(
                    """
                    SELECT team_id::text, min(date)::text AS first_date,
                           max(date)::text AS last_date, count(*) AS report_count
                    FROM daily_reports
                    WHERE team_id::text = ANY(:team_ids)
                    GROUP BY team_id
                    ORDER BY team_id
                    """
                ),
                {"team_ids": team_ids},
            )
        ).mappings().all()
        print(
            json.dumps(
                {
                    "teams": [dict(row) for row in teams],
                    "references": counts,
                    "report_ranges": [dict(row) for row in report_ranges],
                },
                ensure_ascii=False,
                default=str,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
