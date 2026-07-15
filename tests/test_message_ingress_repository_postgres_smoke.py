from __future__ import annotations

from pathlib import Path


def test_repository_postgres_smoke_is_rollback_only() -> None:
    source = Path(
        "scripts/verify_message_ingress_repository_postgres.py"
    ).read_text(encoding="utf-8")

    assert "await session.rollback()" in source
    assert "await session.commit()" not in source
    assert '"business_write_committed": False' in source
    assert "persisted_claims_after_rollback" in source
    assert "persisted_events_after_rollback" in source
