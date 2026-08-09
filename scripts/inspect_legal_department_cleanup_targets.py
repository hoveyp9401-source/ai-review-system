from __future__ import annotations

import asyncio
import json
import re

from sqlalchemy import text

from app.db import AsyncSessionLocal


TARGETS = {
    "canonical_comprehensive": "783ac5ac-527a-40b0-9a3c-fb53d8d4f951",
    "duplicate_comprehensive": "6825dcda-6c35-40d9-83d3-6af93e4a5d47",
    "canonical_fifth": "ef897423-55a4-4924-a3b6-9cd8997f44dc",
    "duplicate_fifth": "1b944b5c-df83-4a91-841b-1e6c75eff53d",
    "person_named_team": "badf2859-3753-40e9-a5c0-fe3bcd52abd1",
}
SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote(value: str) -> str:
    if not SAFE.fullmatch(value):
        raise RuntimeError(f"unsafe identifier: {value}")
    return f'"{value}"'


async def main() -> None:
    async with AsyncSessionLocal() as session:
        columns = (
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
        references = []
        for column in columns:
            schema = str(column["table_schema"])
            table = str(column["table_name"])
            name = str(column["column_name"])
            counts = (
                await session.execute(
                    text(
                        f"SELECT team_id::text AS team_id, count(*) AS row_count "
                        f"FROM {quote(schema)}.{quote(table)} "
                        f"WHERE {quote(name)}::text = ANY(:ids) "
                        f"GROUP BY {quote(name)} ORDER BY {quote(name)}"
                    ),
                    {"ids": list(TARGETS.values())},
                )
            ).mappings().all()
            if counts:
                references.append(
                    {
                        "table": f"{schema}.{table}",
                        "counts": [dict(row) for row in counts],
                    }
                )

        person_team_reports = (
            await session.execute(
                text(
                    """
                    SELECT d.id::text AS report_id, d.date::text AS report_date,
                           d.status, d.user_id::text, u.name AS user_name,
                           u.team_id::text AS user_current_team_id,
                           t.name AS user_current_team_name, t.code AS user_current_team_code
                    FROM daily_reports d
                    JOIN users u ON u.id = d.user_id
                    JOIN teams t ON t.id = u.team_id
                    WHERE d.team_id::text = :person_team_id
                    ORDER BY d.date, d.id
                    """
                ),
                {"person_team_id": TARGETS["person_named_team"]},
            )
        ).mappings().all()

        membership_overlap = (
            await session.execute(
                text(
                    """
                    SELECT dup.membership_id::text AS duplicate_membership_id,
                           canonical.membership_id::text AS canonical_membership_id,
                           dup.user_id::text, u.name AS user_name,
                           dup.tenant_id, dup.effective_from::text
                    FROM legal_daily_team_memberships dup
                    JOIN legal_daily_team_memberships canonical
                      ON canonical.tenant_id = dup.tenant_id
                     AND canonical.user_id = dup.user_id
                     AND canonical.effective_from = dup.effective_from
                     AND canonical.team_id::text = :canonical_id
                    LEFT JOIN users u ON u.id = dup.user_id
                    WHERE dup.team_id::text = :duplicate_id
                    ORDER BY u.name, dup.user_id
                    """
                ),
                {
                    "canonical_id": TARGETS["canonical_comprehensive"],
                    "duplicate_id": TARGETS["duplicate_comprehensive"],
                },
            )
        ).mappings().all()

        membership_roster = (
            await session.execute(
                text(
                    """
                    SELECT m.team_id::text, m.user_id::text, u.name AS user_name,
                           u.team_id::text AS user_current_team_id,
                           current_team.name AS user_current_team_name,
                           m.member_role, m.effective_from::text, m.effective_to::text
                    FROM legal_daily_team_memberships m
                    LEFT JOIN users u ON u.id = m.user_id
                    LEFT JOIN teams current_team ON current_team.id = u.team_id
                    WHERE m.team_id::text IN (:canonical_id, :duplicate_id)
                    ORDER BY m.team_id, u.name, m.user_id
                    """
                ),
                {
                    "canonical_id": TARGETS["canonical_comprehensive"],
                    "duplicate_id": TARGETS["duplicate_comprehensive"],
                },
            )
        ).mappings().all()

        summary_dates = (
            await session.execute(
                text(
                    """
                    SELECT team_id::text, date::text, id::text,
                           report_count, complete_count, generated_at::text
                    FROM team_summaries
                    WHERE team_id::text = ANY(:ids)
                    ORDER BY date, team_id
                    """
                ),
                {
                    "ids": [
                        TARGETS["canonical_comprehensive"],
                        TARGETS["duplicate_comprehensive"],
                        TARGETS["canonical_fifth"],
                        TARGETS["duplicate_fifth"],
                        TARGETS["person_named_team"],
                    ]
                },
            )
        ).mappings().all()

        print(
            json.dumps(
                {
                    "targets": TARGETS,
                    "references": references,
                    "person_named_team_reports": [dict(row) for row in person_team_reports],
                    "comprehensive_membership_overlap": [dict(row) for row in membership_overlap],
                    "comprehensive_membership_roster": [dict(row) for row in membership_roster],
                    "summary_dates": [dict(row) for row in summary_dates],
                },
                ensure_ascii=False,
                default=str,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
