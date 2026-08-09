from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.db import AsyncSessionLocal


CENTER_ID = "142b37a7-02f0-4d62-9c0e-1435292b5207"
PERSON_TEAM_ID = "badf2859-3753-40e9-a5c0-fe3bcd52abd1"


async def main() -> None:
    async with AsyncSessionLocal() as session:
        summaries = (
            await session.execute(
                text(
                    """
                    SELECT team_id::text, date::text, id::text, report_count,
                           complete_count, generated_at::text
                    FROM team_summaries
                    WHERE team_id::text IN (:center_id, :person_team_id)
                    ORDER BY date, team_id
                    """
                ),
                {"center_id": CENTER_ID, "person_team_id": PERSON_TEAM_ID},
            )
        ).mappings().all()
        duplicate_names = (
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
        center_users = (
            await session.execute(
                text(
                    """
                    SELECT u.id::text, u.name, u.active
                    FROM users u
                    WHERE u.team_id::text = :center_id
                    ORDER BY u.name
                    """
                ),
                {"center_id": CENTER_ID},
            )
        ).mappings().all()
        print(
            json.dumps(
                {
                    "summaries": [dict(row) for row in summaries],
                    "active_duplicate_names": [dict(row) for row in duplicate_names],
                    "center_users": [dict(row) for row in center_users],
                },
                ensure_ascii=False,
                default=str,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
