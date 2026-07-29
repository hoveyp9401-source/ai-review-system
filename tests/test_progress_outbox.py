from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.progress import outbox, outbox_status, reconciliation, worker


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def merge(self, event):
        return event


class FakeSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _session_factory(session):
    return lambda: FakeSessionContext(session)


def _settings(**overrides):
    values = {
        "timezone": "Asia/Shanghai",
        "progress_enabled": True,
        "progress_outbox_enabled": True,
        "progress_worker_enabled": True,
        "progress_shadow_only": True,
        "progress_write_updates": False,
        "progress_worker_batch_size": 10,
        "progress_worker_max_retries": 3,
        "progress_worker_stale_lock_minutes": 10,
        "progress_outbox_user_ids": "",
        "progress_outbox_team_ids": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _result(**overrides):
    values = {
        "report_saved": True,
        "report_id": str(uuid4()),
        "report_date": date(2026, 6, 20),
        "status": "collecting",
        "reply_kind": "followup",
        "confirmation_type": "none",
        "confirmed_by_user": False,
        "today_work": ["review contracts"],
        "problems": [],
        "tomorrow_plan": [],
        "quality_warning": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _user():
    return SimpleNamespace(id=uuid4(), team_id=uuid4())


@pytest.mark.asyncio
async def test_progress_feature_flag_disabled_does_not_write_outbox(monkeypatch):
    called = False

    async def fake_create(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(outbox, "create_progress_outbox_event_once", fake_create)

    await outbox.enqueue_daily_report_outbox_best_effort(
        session_factory=_session_factory(FakeSession()),
        settings=_settings(progress_enabled=False),
        user=_user(),
        result=_result(),
        raw_text="hello",
        source="test",
        source_id="message-1",
    )

    assert called is False


@pytest.mark.asyncio
async def test_progress_feature_flag_enabled_writes_outbox(monkeypatch):
    session = FakeSession()
    captured = {}

    async def fake_create(session_arg, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=uuid4()), True

    monkeypatch.setattr(outbox, "create_progress_outbox_event_once", fake_create)
    result = _result()
    user = _user()

    await outbox.enqueue_daily_report_outbox_best_effort(
        session_factory=_session_factory(session),
        settings=_settings(),
        user=user,
        result=result,
        raw_text="today report text",
        source="test",
        source_id="message-1",
    )

    assert session.commits == 1
    assert captured["event_type"] == "daily_report_processed"
    assert captured["source_type"] == "test"
    assert captured["source_id"] == "message-1"
    assert captured["user_id"] == user.id
    assert captured["team_id"] == user.team_id
    assert str(captured["report_id"]) == result.report_id
    assert "raw_text" not in captured["payload_json"]
    assert captured["payload_json"]["raw_text_len"] == len("today report text")
    assert captured["payload_json"]["raw_text_hash"] == captured["raw_text_hash"]
    assert len(captured["raw_text_hash"]) == 64


@pytest.mark.asyncio
async def test_duplicate_report_processing_does_not_insert_duplicate_outbox(monkeypatch):
    session = FakeSession()
    inserted_keys = set()
    insert_count = 0

    async def fake_create(session_arg, **kwargs):
        nonlocal insert_count
        key = kwargs["idempotency_key"]
        if key in inserted_keys:
            return SimpleNamespace(id=uuid4()), False
        inserted_keys.add(key)
        insert_count += 1
        return SimpleNamespace(id=uuid4()), True

    monkeypatch.setattr(outbox, "create_progress_outbox_event_once", fake_create)
    result = _result(report_id=str(uuid4()))
    user = _user()

    for _ in range(2):
        await outbox.enqueue_daily_report_outbox_best_effort(
            session_factory=_session_factory(session),
            settings=_settings(),
            user=user,
            result=result,
            raw_text="same text",
            source="test",
            source_id="same-message",
        )

    assert insert_count == 1
    assert session.commits == 2


@pytest.mark.asyncio
async def test_outbox_write_failure_does_not_raise(monkeypatch):
    session = FakeSession()

    async def fake_create(*args, **kwargs):
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(outbox, "create_progress_outbox_event_once", fake_create)

    await outbox.enqueue_daily_report_outbox_best_effort(
        session_factory=_session_factory(session),
        settings=_settings(),
        user=_user(),
        result=_result(),
        raw_text="will not break report reply",
        source="test",
        source_id="message-1",
    )

    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_canary_user_match_writes_outbox(monkeypatch):
    session = FakeSession()
    user = _user()
    called = False

    async def fake_create(session_arg, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(id=uuid4()), True

    monkeypatch.setattr(outbox, "create_progress_outbox_event_once", fake_create)
    await outbox.enqueue_daily_report_outbox_best_effort(
        session_factory=_session_factory(session),
        settings=_settings(progress_outbox_user_ids=str(user.id)),
        user=user,
        result=_result(),
        raw_text="hello",
        source="test",
        source_id="message-1",
    )

    assert called is True


@pytest.mark.asyncio
async def test_canary_user_miss_does_not_write_outbox(monkeypatch):
    called = False

    async def fake_create(session_arg, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(id=uuid4()), True

    monkeypatch.setattr(outbox, "create_progress_outbox_event_once", fake_create)
    await outbox.enqueue_daily_report_outbox_best_effort(
        session_factory=_session_factory(FakeSession()),
        settings=_settings(progress_outbox_user_ids=str(uuid4())),
        user=_user(),
        result=_result(),
        raw_text="hello",
        source="test",
        source_id="message-1",
    )

    assert called is False


@pytest.mark.asyncio
async def test_worker_repeated_consumption_does_not_reprocess_processed_events(monkeypatch):
    event = SimpleNamespace(id=uuid4(), status="pending", retry_count=0)
    events = [event]
    processed_ids = []

    async def fake_claim(session, *, worker_id, limit, now):
        claimed = [item for item in events if item.status == "pending"]
        for item in claimed:
            item.status = "processing"
        return claimed

    async def fake_recover(session, *, stale_before, now, limit):
        return []

    async def fake_processed(session, event_arg, *, now):
        event_arg.status = "processed"
        processed_ids.append(event_arg.id)

    monkeypatch.setattr(worker, "claim_pending_progress_outbox_events", fake_claim)
    monkeypatch.setattr(worker, "recover_stale_progress_outbox_events", fake_recover)
    monkeypatch.setattr(worker, "mark_progress_outbox_processed", fake_processed)

    session = FakeSession()
    first = await worker.process_progress_outbox_once(
        session_factory=_session_factory(session),
        settings=_settings(),
        worker_id="test-worker",
    )
    second = await worker.process_progress_outbox_once(
        session_factory=_session_factory(session),
        settings=_settings(),
        worker_id="test-worker",
    )

    assert first == 1
    assert second == 0
    assert processed_ids == [event.id]


@pytest.mark.asyncio
async def test_worker_recovers_stale_processing_without_incrementing_retry(monkeypatch):
    event = SimpleNamespace(id=uuid4(), status="processing", retry_count=2, locked_at=datetime(2026, 6, 20, 8, 0))
    recovered_ids = []

    async def fake_recover(session, *, stale_before, now, limit):
        event.status = "pending"
        event.locked_at = None
        recovered_ids.append(event.id)
        return [event]

    async def fake_claim(session, *, worker_id, limit, now):
        return []

    monkeypatch.setattr(worker, "recover_stale_progress_outbox_events", fake_recover)
    monkeypatch.setattr(worker, "claim_pending_progress_outbox_events", fake_claim)

    processed = await worker.process_progress_outbox_once(
        session_factory=_session_factory(FakeSession()),
        settings=_settings(),
        worker_id="test-worker",
    )

    assert processed == 0
    assert recovered_ids == [event.id]
    assert event.retry_count == 2
    assert event.status == "pending"


@pytest.mark.asyncio
async def test_reconciliation_dry_run_does_not_write(monkeypatch):
    record = _reconciliation_record()
    created = False

    async def fake_collect(*, session, plan):
        return [record]

    async def fake_exists(session, idempotency_key):
        return False

    async def fake_create(*args, **kwargs):
        nonlocal created
        created = True

    monkeypatch.setattr(reconciliation, "_collect_reconciliation_records", fake_collect)
    monkeypatch.setattr(reconciliation, "_outbox_exists", fake_exists)
    monkeypatch.setattr(reconciliation, "create_progress_outbox_event_once", fake_create)

    stats = await reconciliation.reconcile_progress_outbox(
        session=FakeSession(),
        plan=reconciliation.ProgressReconciliationPlan(date(2026, 6, 20), date(2026, 6, 20), dry_run=True),
    )

    assert stats.scanned_count == 1
    assert stats.missing_count == 1
    assert stats.created_count == 0
    assert created is False


@pytest.mark.asyncio
async def test_reconciliation_apply_backfills_missing_outbox(monkeypatch):
    record = _reconciliation_record()
    created_keys = []

    async def fake_collect(*, session, plan):
        return [record]

    async def fake_exists(session, idempotency_key):
        return False

    async def fake_create(session, **kwargs):
        created_keys.append(kwargs["idempotency_key"])
        return SimpleNamespace(id=uuid4()), True

    monkeypatch.setattr(reconciliation, "_collect_reconciliation_records", fake_collect)
    monkeypatch.setattr(reconciliation, "_outbox_exists", fake_exists)
    monkeypatch.setattr(reconciliation, "create_progress_outbox_event_once", fake_create)

    stats = await reconciliation.reconcile_progress_outbox(
        session=FakeSession(),
        plan=reconciliation.ProgressReconciliationPlan(date(2026, 6, 20), date(2026, 6, 20), dry_run=False),
    )

    assert stats.created_count == 1
    assert created_keys == [record.idempotency_key]


@pytest.mark.asyncio
async def test_reconciliation_idempotent_skips_existing_outbox(monkeypatch):
    record = _reconciliation_record()

    async def fake_collect(*, session, plan):
        return [record]

    async def fake_exists(session, idempotency_key):
        return True

    monkeypatch.setattr(reconciliation, "_collect_reconciliation_records", fake_collect)
    monkeypatch.setattr(reconciliation, "_outbox_exists", fake_exists)

    stats = await reconciliation.reconcile_progress_outbox(
        session=FakeSession(),
        plan=reconciliation.ProgressReconciliationPlan(date(2026, 6, 20), date(2026, 6, 20), dry_run=False),
    )

    assert stats.scanned_count == 1
    assert stats.missing_count == 0
    assert stats.skipped_count == 1
    assert stats.created_count == 0


def test_outbox_status_format_outputs_required_fields():
    snapshot = {
        "pending": 1,
        "processing": 2,
        "processed": 3,
        "failed": 4,
        "dead_letter": 5,
        "oldest_pending_at": datetime(2026, 6, 20, 9, 0),
        "latest_processed_at": datetime(2026, 6, 20, 10, 0),
        "recent_failed_error": "short error",
    }

    text = outbox_status.format_status(snapshot)

    assert "pending=1" in text
    assert "processing=2" in text
    assert "processed=3" in text
    assert "failed=4" in text
    assert "dead_letter=5" in text
    assert "oldest_pending_at=2026-06-20T09:00:00" in text
    assert "recent_failed_error=short error" in text


def _reconciliation_record():
    return reconciliation.ReconciliationOutboxRecord(
        event_type="daily_report_reconciled",
        source_type="daily_report",
        source_id=str(uuid4()),
        user_id=uuid4(),
        team_id=uuid4(),
        report_id=uuid4(),
        report_date=date(2026, 6, 20),
        raw_text="full original report text",
        payload_json={"raw_text_hash": outbox.raw_text_hash("full original report text"), "raw_text_len": 25},
    )
