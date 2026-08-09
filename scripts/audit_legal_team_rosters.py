from __future__ import annotations

import asyncio
import json
from datetime import date

from sqlalchemy import text

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.services.management_daily_briefing import _load_recipients


ON_DATE = date(2026, 8, 4)


async def main() -> None:
    settings = get_settings()
    tenant_id = str(settings.legal_daily_dashboard_tenant_id).strip()
    async with AsyncSessionLocal() as session:
        team_rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        t.id::text AS team_id,
                        t.code AS team_code,
                        t.name AS team_name,
                        COUNT(*) AS member_count,
                        COUNT(*) FILTER (WHERE u.active IS TRUE) AS active_count,
                        COUNT(*) FILTER (
                            WHERE COALESCE(u.dingtalk_user_id, '') = ''
                        ) AS missing_dingtalk_count,
                        ARRAY_AGG(u.name ORDER BY u.name) AS member_names
                    FROM legal_daily_team_memberships m
                    JOIN teams t ON t.id = m.team_id
                    JOIN users u ON u.id = m.user_id
                    WHERE m.tenant_id = :tenant_id
                      AND m.effective_from <= :on_date
                      AND (m.effective_to IS NULL OR m.effective_to >= :on_date)
                      AND t.active IS TRUE
                    GROUP BY t.id, t.code, t.name
                    ORDER BY t.code, t.name
                    """
                ),
                {"tenant_id": tenant_id, "on_date": ON_DATE},
            )
        ).mappings().all()

        leader_rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        a.dashboard_role,
                        a.team_id::text AS team_id,
                        t.code AS team_code,
                        t.name AS team_name,
                        u.name AS principal_name,
                        u.dingtalk_user_id,
                        a.effective_from,
                        a.effective_to
                    FROM legal_daily_access_assignments a
                    JOIN users u ON u.id::text = a.principal_user_id
                    LEFT JOIN teams t ON t.id = a.team_id
                    WHERE a.tenant_id = :tenant_id
                      AND a.active IS TRUE
                      AND a.effective_from <= :on_date
                      AND (a.effective_to IS NULL OR a.effective_to >= :on_date)
                    ORDER BY a.dashboard_role, t.code, u.name
                    """
                ),
                {"tenant_id": tenant_id, "on_date": ON_DATE},
            )
        ).mappings().all()

        obligation_rows = (
            await session.execute(
                text(
                    """
                    WITH current_members AS (
                        SELECT m.team_id, m.user_id
                        FROM legal_daily_team_memberships m
                        WHERE m.tenant_id = :tenant_id
                          AND m.effective_from <= :on_date
                          AND (
                              m.effective_to IS NULL
                              OR m.effective_to >= :on_date
                          )
                    )
                    SELECT
                        t.id::text AS team_id,
                        t.code AS team_code,
                        t.name AS team_name,
                        COUNT(cm.user_id) AS member_count,
                        COUNT(o.user_id) AS obligation_count,
                        COUNT(o.user_id) FILTER (
                            WHERE o.data_complete IS TRUE
                        ) AS responsibility_complete_count,
                        COUNT(o.user_id) FILTER (
                            WHERE o.required IS TRUE
                        ) AS required_count
                    FROM current_members cm
                    JOIN teams t ON t.id = cm.team_id
                    LEFT JOIN legal_daily_submission_obligations o
                      ON o.tenant_id = :tenant_id
                     AND o.report_date = :on_date
                     AND o.team_id = cm.team_id
                     AND o.user_id = cm.user_id
                    GROUP BY t.id, t.code, t.name
                    ORDER BY t.code, t.name
                    """
                ),
                {"tenant_id": tenant_id, "on_date": ON_DATE},
            )
        ).mappings().all()

        duplicate_rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        u.id::text AS user_id,
                        u.name,
                        COUNT(*) AS team_count,
                        ARRAY_AGG(t.code ORDER BY t.code) AS team_codes
                    FROM legal_daily_team_memberships m
                    JOIN users u ON u.id = m.user_id
                    JOIN teams t ON t.id = m.team_id
                    WHERE m.tenant_id = :tenant_id
                      AND m.effective_from <= :on_date
                      AND (m.effective_to IS NULL OR m.effective_to >= :on_date)
                    GROUP BY u.id, u.name
                    HAVING COUNT(*) <> 1
                    ORDER BY u.name
                    """
                ),
                {"tenant_id": tenant_id, "on_date": ON_DATE},
            )
        ).mappings().all()

        recipients, warnings = await _load_recipients(
            session,
            tenant_id=tenant_id,
            report_date=ON_DATE,
        )

    leaders_by_team: dict[str, list[str]] = {}
    for row in leader_rows:
        if row["dashboard_role"] != "team_lead" or not row["team_id"]:
            continue
        leaders_by_team.setdefault(row["team_id"], []).append(
            row["principal_name"]
        )
    obligations_by_team = {
        row["team_id"]: dict(row) for row in obligation_rows
    }
    recipient_team_ids = {
        recipient.team_ref
        for recipient in recipients
        if recipient.role == "team_lead"
    }
    teams = []
    for row in team_rows:
        obligation = obligations_by_team.get(row["team_id"], {})
        teams.append(
            {
                **dict(row),
                "team_leaders": leaders_by_team.get(row["team_id"], []),
                "briefing_recipient_ready": row["team_id"]
                in recipient_team_ids,
                "obligation_count": obligation.get("obligation_count", 0),
                "responsibility_complete_count": obligation.get(
                    "responsibility_complete_count", 0
                ),
                "required_count": obligation.get("required_count", 0),
            }
        )
    print(
        json.dumps(
            {
                "date": ON_DATE.isoformat(),
                "tenant_id": tenant_id,
                "teams": teams,
                "access_assignments": [dict(row) for row in leader_rows],
                "duplicate_or_missing_team_memberships": [
                    dict(row) for row in duplicate_rows
                ],
                "recipient_warning_count": len(warnings),
                "recipient_warnings": list(warnings),
            },
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
