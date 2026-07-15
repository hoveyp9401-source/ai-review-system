from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.business.travel_pipeline import (
    REAL_USER_TRAVEL_SOURCE_CHANNELS,
    evaluate_travel_collaboration_candidates,
)


class _Rows:
    rowcount = 0


class _EmptyScalars:
    @staticmethod
    def all():
        return []


class _RecordingSession:
    def __init__(self):
        self.scalar_statement = None

    async def execute(self, _statement):
        return _Rows()

    async def scalars(self, statement):
        self.scalar_statement = statement
        return _EmptyScalars()

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_travel_candidate_scan_hard_filters_non_user_fixture_sources():
    session = _RecordingSession()

    result = await evaluate_travel_collaboration_candidates(  # type: ignore[arg-type]
        session,
        now=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
        allowed_tenant_ids=("sandbox-agent2-phase2-20260711",),
    )

    assert result.scanned_intents == 0
    compiled = session.scalar_statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "agent2_travel_intents.source_channel IN" in sql
    assert set(compiled.params["source_channel_1"]) == set(REAL_USER_TRAVEL_SOURCE_CHANNELS)
    assert "server_acceptance_smoke" not in REAL_USER_TRAVEL_SOURCE_CHANNELS
