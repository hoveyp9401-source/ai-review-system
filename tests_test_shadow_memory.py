from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import repositories


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _CollectingSession:
    def __init__(self):
        self.added = []
        self.flush_count = 0
        self.begin_nested_count = 0

    def begin_nested(self):
        self.begin_nested_count += 1
        return _Nested()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flush_count += 1


class _BrokenSession(_CollectingSession):
    def begin_nested(self):
        raise RuntimeError("shadow table unavailable")


def _settings(enabled: bool):
    return SimpleNamespace(shadow_memory_enabled=enabled)


def _report(**overrides):
    values = {
        "id": uuid4(),
        "today_work": ["完成协议用印3个"],
        "problems": [],
        "tomorrow_plan": [],
        "section_status": {
            "_draft_item_ids": {
                "today_work": ["item-1"],
                "problems": [],
                "tomorrow_plan": [],
            }
        },
        "status": "collecting",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_shadow_memory_disabled_does_not_write(monkeypatch):
    monkeypatch.setattr(repositories, "get_settings", lambda: _settings(False))
    session = _BrokenSession()
    user = SimpleNamespace(id=uuid4(), dingtalk_user_id="user-001")

    await repositories.maybe_create_report_interaction_event(
        session,
        user=user,
        report=_report(),
        report_date=date(2026, 6, 18),
        message_text="第一条不是用眼是用印",
        llm_decision_json={"operation": "rewrite_item", "confidence": 0.9},
        backend_action="rewrite_item",
        before_snapshot_json={},
        after_snapshot_json={},
    )

    assert session.added == []
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_shadow_memory_event_records_correction_and_undo(monkeypatch):
    monkeypatch.setattr(repositories, "get_settings", lambda: _settings(True))
    session = _CollectingSession()
    user = SimpleNamespace(id=uuid4(), dingtalk_user_id="user-001")
    report = _report()
    before = repositories.build_report_interaction_snapshot(report)
    after = repositories.build_report_interaction_snapshot(
        _report(today_work=["完成协议用印13个"])
    )

    await repositories.maybe_create_report_interaction_event(
        session,
        user=user,
        report=report,
        report_date=date(2026, 6, 18),
        message_text="撤回上一步，第一条不是用眼是用印",
        llm_decision_json={
            "operation": "restore_previous",
            "confidence": 0.95,
            "restore_previous": {"enabled": True},
        },
        backend_action="restore_previous",
        before_snapshot_json=before,
        after_snapshot_json=after,
    )

    assert session.begin_nested_count == 1
    assert session.flush_count == 1
    assert len(session.added) == 1
    event = session.added[0]
    assert event.dingtalk_user_id == "user-001"
    assert event.report_date == date(2026, 6, 18)
    assert event.backend_action == "restore_previous"
    assert event.correction_type == "asr_correction"
    assert event.correction_from == "用眼"
    assert event.correction_to == "用印"
    assert event.is_undo is True
    assert event.asr_suspect_json["from"] == "用眼"
    assert event.before_snapshot_json["today_work_count"] == 1
    assert event.after_snapshot_json["today_work"] == ["完成协议用印13个"]


@pytest.mark.asyncio
async def test_shadow_memory_write_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(repositories, "get_settings", lambda: _settings(True))
    session = _BrokenSession()
    user = SimpleNamespace(id=uuid4(), dingtalk_user_id="user-001")

    await repositories.maybe_create_report_interaction_event(
        session,
        user=user,
        report=_report(),
        report_date=date(2026, 6, 18),
        message_text="修正第二项数量",
        llm_decision_json={"operation": "rewrite_item", "confidence": 0.9},
        backend_action="rewrite_item",
        before_snapshot_json={},
        after_snapshot_json={},
    )

    assert session.added == []


def test_shadow_snapshot_and_backend_action_are_summaries():
    report = _report(today_work=[f"事项{i}" for i in range(25)])
    snapshot = repositories.build_report_interaction_snapshot(report)

    assert snapshot["today_work_count"] == 25
    assert len(snapshot["today_work"]) == 20
    assert snapshot["item_ids"]["today_work"] == ["item-1"]
    assert repositories.infer_backend_action(
        llm_payload={"operation": "move_item"},
        source="test",
        status="collecting",
    ) == "move_item"
