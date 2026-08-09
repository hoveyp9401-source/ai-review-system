from __future__ import annotations

import asyncio
import json
from datetime import date

from sqlalchemy import text

from app.config import get_settings
from app.db import AsyncSessionLocal


ON_DATE = date(2026, 8, 6)
PARENT = "法务合约中心"


async def main() -> None:
    settings = get_settings()
    tenant_id = str(settings.legal_daily_dashboard_tenant_id).strip()
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    WITH active_users AS (
                        SELECT u.team_id, u.id AS user_id, u.name
                        FROM users u
                        JOIN teams t ON t.id = u.team_id
                        WHERE u.active IS TRUE
                          AND t.active IS TRUE
                          AND t.department_name = :parent
                    ),
                    current_memberships AS (
                        SELECT m.team_id, m.user_id, u.name, m.source,
                               m.data_complete
                        FROM legal_daily_team_memberships m
                        JOIN users u ON u.id = m.user_id
                        JOIN teams t ON t.id = m.team_id
                        WHERE m.tenant_id = :tenant_id
                          AND m.effective_from <= :on_date
                          AND (m.effective_to IS NULL OR m.effective_to >= :on_date)
                          AND t.active IS TRUE
                          AND t.department_name = :parent
                    )
                    SELECT t.id::text AS team_id, t.code, t.name,
                           (SELECT count(*) FROM active_users au WHERE au.team_id = t.id) AS active_users,
                           (SELECT count(*) FROM current_memberships cm WHERE cm.team_id = t.id) AS current_memberships,
                           COALESCE((
                               SELECT jsonb_agg(au.name ORDER BY au.name)
                               FROM active_users au
                               WHERE au.team_id = t.id
                                 AND NOT EXISTS (
                                     SELECT 1 FROM current_memberships cm
                                     WHERE cm.user_id = au.user_id AND cm.team_id = au.team_id
                                 )
                           ), '[]'::jsonb) AS missing_from_memberships,
                           COALESCE((
                               SELECT jsonb_agg(cm.name ORDER BY cm.name)
                               FROM current_memberships cm
                               WHERE cm.team_id = t.id
                                 AND NOT EXISTS (
                                     SELECT 1 FROM active_users au
                                     WHERE au.user_id = cm.user_id AND au.team_id = cm.team_id
                                 )
                           ), '[]'::jsonb) AS extra_in_memberships,
                           COALESCE((
                               SELECT jsonb_agg(DISTINCT jsonb_build_object(
                                   'source', cm.source,
                                   'data_complete', cm.data_complete
                               ))
                               FROM current_memberships cm
                               WHERE cm.team_id = t.id
                           ), '[]'::jsonb) AS membership_sources
                    FROM teams t
                    WHERE t.active IS TRUE AND t.department_name = :parent
                    ORDER BY t.name, t.code
                    """
                ),
                {"tenant_id": tenant_id, "on_date": ON_DATE, "parent": PARENT},
            )
        ).mappings().all()
        multi_or_missing = (
            await session.execute(
                text(
                    """
                    WITH current_memberships AS (
                        SELECT m.user_id, m.team_id
                        FROM legal_daily_team_memberships m
                        JOIN teams t ON t.id = m.team_id
                        WHERE m.tenant_id = :tenant_id
                          AND m.effective_from <= :on_date
                          AND (m.effective_to IS NULL OR m.effective_to >= :on_date)
                          AND t.active IS TRUE
                          AND t.department_name = :parent
                    )
                    SELECT u.id::text AS user_id, u.name,
                           u.team_id::text AS expected_team_id,
                           count(cm.team_id) AS membership_count,
                           array_agg(cm.team_id::text ORDER BY cm.team_id)
                               FILTER (WHERE cm.team_id IS NOT NULL) AS membership_team_ids
                    FROM users u
                    JOIN teams t ON t.id = u.team_id
                    LEFT JOIN current_memberships cm ON cm.user_id = u.id
                    WHERE u.active IS TRUE
                      AND t.active IS TRUE
                      AND t.department_name = :parent
                    GROUP BY u.id, u.name, u.team_id
                    HAVING count(cm.team_id) <> 1
                        OR bool_or(cm.team_id IS DISTINCT FROM u.team_id)
                    ORDER BY u.name
                    """
                ),
                {"tenant_id": tenant_id, "on_date": ON_DATE, "parent": PARENT},
            )
        ).mappings().all()
        roster = (
            await session.execute(
                text(
                    """
                    SELECT t.id::text AS team_id, t.name AS team_name,
                           u.id::text AS user_id, u.name AS user_name,
                           m.source, m.data_complete,
                           m.effective_from::text, m.effective_to::text
                    FROM legal_daily_team_memberships m
                    JOIN users u ON u.id = m.user_id
                    JOIN teams t ON t.id = m.team_id
                    WHERE m.tenant_id = :tenant_id
                      AND m.effective_from <= :on_date
                      AND (m.effective_to IS NULL OR m.effective_to >= :on_date)
                      AND t.active IS TRUE
                      AND t.department_name = :parent
                    ORDER BY t.name, u.name
                    """
                ),
                {"tenant_id": tenant_id, "on_date": ON_DATE, "parent": PARENT},
            )
        ).mappings().all()
        user_team_mismatches = (
            await session.execute(
                text(
                    """
                    SELECT u.id::text AS user_id, u.name AS user_name,
                           u.team_id::text AS user_team_id,
                           m.team_id::text AS roster_team_id,
                           t.name AS roster_team_name
                    FROM legal_daily_team_memberships m
                    JOIN users u ON u.id = m.user_id
                    JOIN teams t ON t.id = m.team_id
                    WHERE m.tenant_id = :tenant_id
                      AND m.effective_from <= :on_date
                      AND (m.effective_to IS NULL OR m.effective_to >= :on_date)
                      AND t.active IS TRUE
                      AND t.department_name = :parent
                      AND u.team_id IS DISTINCT FROM m.team_id
                    ORDER BY u.name
                    """
                ),
                {"tenant_id": tenant_id, "on_date": ON_DATE, "parent": PARENT},
            )
        ).mappings().all()
        print(
            json.dumps(
                {
                    "date": ON_DATE.isoformat(),
                    "tenant_id": tenant_id,
                    "teams": [dict(row) for row in rows],
                    "multi_missing_or_wrong_memberships": [
                        dict(row) for row in multi_or_missing
                    ],
                    "current_roster": [dict(row) for row in roster],
                    "user_team_mismatches": [
                        dict(row) for row in user_team_mismatches
                    ],
                },
                ensure_ascii=False,
                default=str,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
