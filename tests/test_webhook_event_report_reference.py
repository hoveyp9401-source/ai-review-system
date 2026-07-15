from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repositories import mark_webhook_event_processed


@pytest.mark.asyncio
async def test_webhook_event_does_not_link_non_legacy_report_id() -> None:
    class Session:
        flushed = False

        async def get(self, _model, _object_id):
            return None

        async def flush(self):
            self.flushed = True

    session = Session()
    event = SimpleNamespace(
        status="processing",
        report_id=None,
        response_payload={},
        processed_at=None,
        error_message="old-error",
    )

    await mark_webhook_event_processed(
        session,  # type: ignore[arg-type]
        event,
        report_id=UUID("27436a7c-251c-5359-b515-b33fa7ea4b5a"),
        response_payload={"msgtype": "text", "text": {"content": "查询完成"}},
        now=datetime(2026, 7, 14, 11, 7, tzinfo=UTC),
    )

    assert event.status == "processed"
    assert event.report_id is None
    assert session.flushed is True
