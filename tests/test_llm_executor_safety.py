from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas import DailyInputIntentDecision, StructuredDailyReport
from app.services import report_service
from app.services.report_service import DailyReportService
from app.services.state_machine import CONFIRMATION_NONE, STATUS_COLLECTING


class FakeExtractor:
    def __init__(self, outputs=None, intent_outputs=None):
        self.outputs = list(outputs or [])
        self.intent_outputs = list(intent_outputs or [])
        self.client = SimpleNamespace(model="fake")

    async def extract(self, raw_input):
        if self.outputs:
            return self.outputs.pop(0)
        return StructuredDailyReport()

    async def decide_intent(self, *, raw_input, context):
        if self.intent_outputs:
            return self.intent_outputs.pop(0)
        return DailyInputIntentDecision(intent="continue_collecting", confidence=0.8)


class FakeSession:
    def add(self, obj):
        self.obj = obj

    async def flush(self):
        return None


def _user():
    return SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")


def _make_report(**overrides):
    values = {
        "id": uuid4(),
        "report_date": date(2026, 6, 16),
        "today_work": [],
        "problems": [],
        "tomorrow_plan": [],
        "section_status": {},
        "status": STATUS_COLLECTING,
        "confirmation_type": CONFIRMATION_NONE,
        "confirmed_by_user": False,
        "quality_warning": None,
        "emotion": "",
        "llm_model": "fake",
        "llm_payload": {},
        "source": "test",
        "submitted_at": None,
        "pending_confirmation_at": None,
        "auto_submit_at": None,
        "last_modified_by_user": False,
        "last_modified_at": None,
        "completeness_score": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_store(monkeypatch, initial=None):
    stored = {"report": initial}

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        previous = stored["report"]
        stored["report"] = _make_report(
            id=previous.id if previous else uuid4(),
            report_date=kwargs["report_date"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            completeness_score=kwargs["completeness_score"],
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
            emotion=kwargs["emotion"],
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
        )
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 16))
    return stored


def test_structured_report_extracts_text_from_indexed_item_dicts():
    report = StructuredDailyReport(
        today_work=[{"index": 1, "text": "drafted memo"}],
        problems=[{"index": 1, "text": "missing evidence"}],
        tomorrow_plan=[{"index": 1, "text": "attend hearing"}],
    )

    assert report.today_work == ["drafted memo"]
    assert report.problems == ["missing evidence"]
    assert report.tomorrow_plan == ["attend hearing"]


@pytest.mark.asyncio
async def test_router_targeted_problem_appends_when_problem_field_already_exists(monkeypatch):
    existing = _make_report(
        today_work=["drafted memo"],
        problems=["old risk"],
        tomorrow_plan=[],
        section_status={"today_work": True, "problems": True, "tomorrow_plan": False},
        completeness_score=0.67,
    )
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        outputs=[StructuredDailyReport(problems=[{"index": 1, "text": "new risk"}], completeness=0.33)],
        intent_outputs=[
            DailyInputIntentDecision(
                message_kind="report_content",
                intent="continue_collecting",
                operation="set_fields",
                target_field="problems",
                should_update_report=True,
                confidence=0.9,
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="new risk", source="test")

    assert result.problems == ["old risk", "new risk"]
    assert "{'index'" not in result.message
