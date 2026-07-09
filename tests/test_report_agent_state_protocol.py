import logging
import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent.action_plan import ActionPlan, AgentAction, PendingInteractionPlan
from app.agent import executor as agent_executor
from app.agent.report_agent import AgentDecisionResult
from app.services import report_service
from app.services.report_service import DailyReportService
from app.services.state_machine import CONFIRMATION_NONE, STATUS_COLLECTING, STATUS_COMPLETED, STATUS_PENDING_CONFIRMATION

_ORIGINAL_NOW_IN_TIMEZONE = report_service.now_in_timezone


class FakeSession:
    async def flush(self):
        return None


class FakeExtractor:
    def __init__(self):
        self.client = SimpleNamespace(settings=SimpleNamespace(), model="fake-client")


class FakeAgent:
    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []

    async def decide_with_meta(self, *, raw_input, context):
        self.calls.append({"raw_input": raw_input, "context": context})
        if not self.plans:
            raise AssertionError("unexpected agent call")
        return AgentDecisionResult(payload=self.plans.pop(0), meta={"model": "fake-agent", "thinking": False, "timeout": False})


def _settings():
    return SimpleNamespace(timezone="Asia/Shanghai", report_agent_enabled=True)


_TEST_USER_ID = uuid4()
_TEST_TEAM_ID = uuid4()


def _user():
    return SimpleNamespace(id=_TEST_USER_ID, team_id=_TEST_TEAM_ID, timezone="Asia/Shanghai", name="庞浩")


def _report(**overrides):
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
        "completeness_score": 0.0,
        "input_fragments": [],
        "llm_model": "fake",
        "llm_payload": {},
        "source": "test",
        "submitted_at": None,
        "last_modified_by_user": False,
        "last_modified_at": None,
        "auto_submit_at": None,
        "pending_confirmation_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_store(monkeypatch, initial=None, previous=None):
    stored = {"report": initial, "previous": previous}

    async def fake_lock(session, user_id, report_date):
        return None

    async def fake_get_report(session, user_id, report_date):
        if report_date == date(2026, 6, 15):
            return stored["previous"]
        return stored["report"]

    async def fake_set_pending_interaction(session, report, pending_interaction):
        section_status = dict(report.section_status or {})
        if pending_interaction:
            section_status["_pending_interaction"] = pending_interaction
        else:
            section_status.pop("_pending_interaction", None)
        report.section_status = section_status
        stored["report"] = report
        return report

    async def fake_upsert_daily_report(session, **kwargs):
        bucket = "previous" if kwargs["report_date"] == date(2026, 6, 15) else "report"
        previous = stored[bucket]
        fragments = list(getattr(previous, "input_fragments", []) or [])
        fragments.append({"raw_input": kwargs["raw_input"], "structured": kwargs["llm_payload"]})
        stored[bucket] = _report(
            id=previous.id if previous else uuid4(),
            report_date=kwargs["report_date"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            completeness_score=kwargs["completeness_score"],
            input_fragments=fragments,
        )
        return stored[bucket]

    monkeypatch.setattr(report_service, "_acquire_report_processing_lock", fake_lock)
    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "_set_pending_interaction", fake_set_pending_interaction)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 16))
    if report_service.now_in_timezone is _ORIGINAL_NOW_IN_TIMEZONE:
        monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 8, 0))
    monkeypatch.setattr(agent_executor, "get_report", fake_get_report)
    monkeypatch.setattr(agent_executor, "upsert_daily_report", fake_upsert_daily_report)
    return stored



def _install_multiday_store(monkeypatch, reports_by_date):
    stored = {key: value for key, value in reports_by_date.items()}

    async def fake_lock(session, user_id, report_date):
        return None

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_set_pending_interaction(session, report, pending_interaction):
        section_status = dict(report.section_status or {})
        if pending_interaction:
            section_status["_pending_interaction"] = pending_interaction
        else:
            section_status.pop("_pending_interaction", None)
        report.section_status = section_status
        stored[report.report_date] = report
        return report

    async def fake_upsert_daily_report(session, **kwargs):
        report_date = kwargs["report_date"]
        previous = stored.get(report_date)
        fragments = list(getattr(previous, "input_fragments", []) or [])
        fragments.append({"raw_input": kwargs["raw_input"], "structured": kwargs["llm_payload"]})
        stored[report_date] = _report(
            id=previous.id if previous else uuid4(),
            report_date=report_date,
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            completeness_score=kwargs["completeness_score"],
            input_fragments=fragments,
        )
        return stored[report_date]

    monkeypatch.setattr(report_service, "_acquire_report_processing_lock", fake_lock)
    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "_set_pending_interaction", fake_set_pending_interaction)
    monkeypatch.setattr(agent_executor, "get_report", fake_get_report)
    monkeypatch.setattr(agent_executor, "upsert_daily_report", fake_upsert_daily_report)
    return stored

def _assert_historical_cursor(store, field: str, *, target_date: str = "2026-06-15"):
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "historical_report_edit_flow"
    assert pending["target_field"] == field
    assert pending["context"]["target_date"] == target_date
    if field in {"today_work", "problems", "tomorrow_plan"}:
        assert pending["context"]["focus_section"] == field
    else:
        assert pending["context"].get("focus_section", "none") == "none"
    assert pending["context"]["edit_cursor"]["target_date"] == target_date
    assert pending["context"]["edit_cursor"]["focused_section"] == field
    return pending


def _assert_current_cursor(store, field: str, *, target_date: str = "2026-06-16"):
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "current_report_edit_flow"
    assert pending["target_field"] == field
    assert pending["context"]["target_date"] == target_date
    assert pending["context"]["edit_cursor"]["mode"] == "current_edit"
    assert pending["context"]["edit_cursor"]["target_date"] == target_date
    assert pending["context"]["edit_cursor"]["focused_section"] == field
    return pending


def test_weekend_default_report_date_uses_friday_only_before_saturday_cutoff():
    assert report_service._default_report_date_for_received_at(datetime(2026, 6, 27, 8, 59)) == date(2026, 6, 26)
    assert report_service._default_report_date_for_received_at(datetime(2026, 6, 27, 9, 0)) == date(2026, 6, 27)
    assert report_service._default_report_date_for_received_at(datetime(2026, 6, 28, 8, 30)) == date(2026, 6, 28)


def test_previous_content_cutoff_uses_calendar_yesterday_before_nine():
    raw_input = "\u6628\u5929\u5ba1\u6838\u4e86\u5408\u540c\u3002"

    assert not report_service._is_previous_report_blocked_by_cutoff(
        raw_input,
        date(2026, 6, 17),
        datetime(2026, 6, 18, 8, 30),
    )
    assert report_service._is_previous_report_blocked_by_cutoff(
        raw_input,
        date(2026, 6, 18),
        datetime(2026, 6, 18, 20, 11),
    )


@pytest.mark.asyncio
async def test_non_reporting_day_does_not_create_current_report(monkeypatch):
    store = _install_multiday_store(monkeypatch, {})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 28))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 28, 14, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="休假", source="test")

    assert result.reply_kind == "non_reporting_day"
    assert result.report_saved is True
    assert result.report_id is None
    assert store == {}


@pytest.mark.asyncio
async def test_no_remaining_content_fills_missing_sections_as_empty(monkeypatch):
    existing = _report(
        today_work=["整理案件资料"],
        problems=[],
        tomorrow_plan=[],
        section_status={"today_work": True, "problems": False, "tomorrow_plan": False},
        status=STATUS_COLLECTING,
        completeness_score=0.3333,
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="其他没有了", source="test")

    assert result.status == STATUS_PENDING_CONFIRMATION
    assert result.missing_sections == []
    assert result.today_work == ["整理案件资料"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["无明日计划"]
    assert result.section_status["problems_acknowledged_empty"] is True
    assert result.section_status["tomorrow_plan_acknowledged_empty"] is True
    assert store["report"].status == STATUS_PENDING_CONFIRMATION


@pytest.mark.asyncio
async def test_pending_target_confirmation_inherits_section_and_asks_content(monkeypatch):
    existing = _report(
        today_work=["审核18份合同"],
        problems=[],
        tomorrow_plan=[],
        section_status={"_pending_interaction": {"type": "awaiting_append_target_confirmation", "operation": "append", "target_field": "problems"}},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="对", source="test")

    assert result.report_saved is True
    assert "补充的问题内容" in result.message
    assert store["report"].section_status["_pending_interaction"]["type"] == "awaiting_append_content"
    assert store["report"].section_status["_pending_interaction"]["target_field"] == "problems"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input, field, expected",
    [
        ("问题吧", "problems", "补充的问题内容"),
        ("风险吧", "problems", "补充的问题内容"),
        ("明天吧", "tomorrow_plan", "补充的明日计划内容"),
        ("工作吧", "today_work", "补充的今日工作内容"),
    ],
)
async def test_state_resolver_selected_target_sets_awaiting_content(monkeypatch, raw_input, field, expected):
    existing = _report(section_status={"_pending_interaction": {"type": "awaiting_append_target", "operation": "append"}})
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert expected in result.message
    assert store["report"].section_status["_pending_interaction"]["target_field"] == field
    assert store["report"].today_work == []
    assert store["report"].problems == []
    assert store["report"].tomorrow_plan == []


@pytest.mark.asyncio
async def test_awaiting_content_confirmation_reprompts_without_writing(monkeypatch):
    existing = _report(section_status={"_pending_interaction": {"type": "awaiting_append_content", "operation": "append", "target_field": "problems"}})
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="对", source="test")

    assert result.report_saved is True
    assert "补充的问题内容" in result.message
    assert store["report"].problems == []
    assert store["report"].section_status["_pending_interaction"]["target_field"] == "problems"


def test_existing_append_content_state_promotes_substantive_problem_reply(monkeypatch):
    existing = _report(
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_append_content",
                "operation": "append",
                "target_field": "problems",
            },
            "problems_acknowledged_empty": True,
        },
    )
    store = _install_store(monkeypatch, existing)
    executor = agent_executor.ReportAgentExecutor()
    plan = ActionPlan(
        intent="unclear",
        confidence="high",
        should_write=False,
        reply_to_user="\u597d\u7684\uff0c\u8bf7\u8bf4\u5177\u4f53\u662f\u4ec0\u4e48\u98ce\u9669\u6216\u95ee\u9898\u3002",
        pending_interaction_to_set=PendingInteractionPlan(
            type="awaiting_append_content",
            operation="append",
            target_field="problems",
        ),
    )

    result = asyncio.run(
        executor.execute(
            FakeSession(),
            user=_user(),
            existing=existing,
            report_date=date(2026, 6, 16),
            received_at=datetime(2026, 6, 16, 8, 0),
            raw_input="\u4f9b\u5e94\u5546\u8d44\u6599\u6ca1\u6709\u53d1\u5168",
            source="test",
            plan=plan,
            meta={"model": "fake"},
        )
    )

    assert result.report_saved is True
    assert any("\u4f9b\u5e94\u5546\u8d44\u6599\u6ca1\u6709\u53d1\u5168" in item for item in store["report"].problems)
    assert "_pending_interaction" not in store["report"].section_status



@pytest.mark.asyncio
async def test_expected_tomorrow_slot_short_answer_updates_plan_without_reasking_target(monkeypatch):
    existing = _report(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=[],
        section_status={"problems_acknowledged_empty": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u5199\u62a5\u544a", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["\u5199\u62a5\u544a"]
    assert not service.report_agent.calls
    assert "\u8865\u5230\u54ea\u4e00\u680f" not in result.message


@pytest.mark.asyncio
async def test_followup_marker_while_awaiting_plan_appends_to_last_today_work(monkeypatch):
    existing = _report(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=[],
        section_status={"problems_acknowledged_empty": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u7b49\u7b49\uff0c\u6211\u8fd8\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["\u5ba1\u6838\u5408\u540c", "\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a"]
    assert store["report"].tomorrow_plan == []
    assert not service.report_agent.calls
    assert "\u8865\u5230\u54ea\u4e00\u680f" not in result.message


@pytest.mark.asyncio
async def test_append_content_done_phrase_clears_pending_without_target_reprompt(monkeypatch):
    existing = _report(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        section_status={"_pending_interaction": {"type": "awaiting_append_content", "operation": "append", "target_field": "today_work"}},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u4e0d\u7528\u4e86", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["\u5ba1\u6838\u5408\u540c"]
    assert "_pending_interaction" not in store["report"].section_status
    assert not service.report_agent.calls
    assert "\u8865\u5230\u54ea\u4e00\u680f" not in result.message


@pytest.mark.asyncio
async def test_pure_repair_feedback_does_not_enter_report(monkeypatch):
    existing = _report(today_work=["\u5ba1\u6838\u5408\u540c"], problems=[], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u5b7a\u5b50\u4e0d\u53ef\u6559", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["\u5ba1\u6838\u5408\u540c"]
    assert store["report"].problems == []
    assert store["report"].tomorrow_plan == []
    assert not service.report_agent.calls
    assert "\u4e0d\u4f1a\u5199\u5165\u65e5\u62a5" in result.message


@pytest.mark.asyncio
async def test_repair_feedback_clears_interruptible_pending_without_writing(monkeypatch):
    existing = _report(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        section_status={"_pending_interaction": {"type": "pending_clarification", "operation": "merge", "target_field": "today_work"}},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u9519\u4e86", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["\u5ba1\u6838\u5408\u540c"]
    assert "_pending_interaction" not in store["report"].section_status
    assert not service.report_agent.calls
    assert "\u4e0d\u4f1a\u5199\u5165\u65e5\u62a5" in result.message


@pytest.mark.asyncio
async def test_repair_phrase_with_replacement_content_still_uses_edit_path(monkeypatch):
    existing = _report(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        section_status={"_last_modified_item": {"field": "today_work", "item_index": 1}},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="replace_field", field="today_work", items=["\u53c2\u52a0\u8bc4\u5ba1\u4f1a"])],
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u521a\u624d\u90a3\u6761\u6539\u6210\u53c2\u52a0\u8bc4\u5ba1\u4f1a",
        source="test",
    )

    assert result.report_saved is True
    assert store["report"].today_work == ["\u53c2\u52a0\u8bc4\u5ba1\u4f1a"]
    assert service.report_agent.calls



@pytest.mark.asyncio
async def test_quality_clarification_accepts_original_candidate(monkeypatch):
    existing = _report(
        today_work=["审核30份材料合同", "完成计划去游泳"],
        section_status={"_pending_interaction": {"type": "awaiting_append_content", "operation": "append", "target_field": "problems"}},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="medium",
                should_write=False,
                actions=[AgentAction(type="ask_clarification")],
                reply_to_user="请具体描述您发现的问题。",
                reason="Problem content is vague.",
            )
        ]
    )

    first = await service.submit_text(FakeSession(), user=_user(), raw_input="发现了很大的问题", source="test")

    assert first.report_saved is True
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "awaiting_content_quality_confirmation"
    assert pending["target_field"] == "problems"
    assert pending["context"]["candidate_items"] == ["发现了很大的问题"]

    second = await service.submit_text(FakeSession(), user=_user(), raw_input="就这么写", source="test")

    assert second.report_saved is True
    assert store["report"].problems == ["发现了很大的问题"]
    assert "就这么写" not in store["report"].problems
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_awaiting_content_does_not_regress_to_target_selection(monkeypatch):
    existing = _report(
        today_work=["审核30份材料合同"],
        section_status={"_pending_interaction": {"type": "awaiting_append_content", "operation": "append", "target_field": "problems"}},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="medium",
                should_write=False,
                actions=[AgentAction(type="ask_clarification")],
                reply_to_user="您想补充到哪个部分？今日工作、问题还是明日计划？",
                pending_interaction_to_set=PendingInteractionPlan(type="awaiting_append_target", operation="append", target_field="none"),
            )
        ]
    )

    first = await service.submit_text(FakeSession(), user=_user(), raw_input="发现了很大的问题", source="test")

    assert first.report_saved is True
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "awaiting_content_quality_confirmation"
    assert pending["target_field"] == "problems"
    assert pending["context"]["candidate_items"] == ["发现了很大的问题"]

    second = await service.submit_text(FakeSession(), user=_user(), raw_input="就这么写", source="test")

    assert second.report_saved is True
    assert store["report"].problems == ["发现了很大的问题"]
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_append_target_confirmation_with_candidate_accepts_original(monkeypatch):
    existing = _report(
        today_work=["审核30份材料合同"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_append_target_confirmation",
                "operation": "append",
                "target_field": "problems",
                "context": {"candidate_items": ["发现了很大的问题"]},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="就这么写", source="test")

    assert result.report_saved is True
    assert store["report"].problems == ["发现了很大的问题"]
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_dated_report_modify_context_is_used_for_followup_display(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["昨天审核合同A"],
        problems=["昨天无明显问题"],
        tomorrow_plan=["今天跟进合同B"],
        status=STATUS_COMPLETED,
    )
    store = _install_store(monkeypatch, initial=None, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="medium",
                should_write=False,
                reply_to_user="您想修改昨天日志的哪个部分？是今日工作、问题还是明日计划？",
                reason="Need section for dated report modification.",
            )
        ]
    )

    first = await service.submit_text(FakeSession(), user=_user(), raw_input="我想改昨天的日志", source="test")

    assert first.report_saved is True
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "historical_report_edit_flow"
    assert pending["context"]["target_date"] == "2026-06-15"

    second = await service.submit_text(FakeSession(), user=_user(), raw_input="你先发我看看", source="test")

    assert second.report_saved is False
    assert "2026-06-15" in second.message
    assert "昨天审核合同A" in second.message
    assert "2026-06-16" not in second.message


@pytest.mark.asyncio
async def test_historical_report_edit_entry_displays_yesterday_first(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["昨天审核合同A"],
        problems=["昨天无明显问题"],
        tomorrow_plan=["今天跟进合同B"],
        status=STATUS_COMPLETED,
    )
    store = _install_store(monkeypatch, initial=None, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="改下昨天的日报", source="test")

    assert result.report_saved is True
    assert "我先把昨天的日报调出来" in result.message
    assert "昨天审核合同A" in result.message
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "historical_report_edit_flow"
    assert pending["context"]["target_date"] == "2026-06-15"
    assert pending["context"]["stage"] == "awaiting_edit_instruction"
    assert pending["context"]["current_report"]["problems"] == ["昨天无明显问题"]


@pytest.mark.asyncio
async def test_current_report_edit_entry_displays_today_first(monkeypatch):
    current = _report(
        today_work=["审核30份增补合同", "发送20余份邮件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_PENDING_CONFIRMATION,
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="今天的日报我改下", source="test")

    assert result.report_saved is True
    assert "我先把今天的日报调出来" in result.message
    assert "今日日报（2026-06-16）" in result.message
    assert "发送20余份邮件" in result.message
    assert "您想修改今天的日报哪个部分" not in result.message
    assert service.report_agent.calls == []
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "current_report_edit_flow"
    assert pending["context"]["target_date"] == "2026-06-16"
    assert pending["context"]["edit_cursor"]["mode"] == "current_edit"
    assert pending["context"]["edit_cursor"]["focused_section"] == "none"
    assert pending["context"]["current_report"]["tomorrow_plan"] == ["计划去游泳"]


@pytest.mark.asyncio
async def test_current_report_field_selection_keeps_focus(monkeypatch):
    current = _report(
        today_work=["审核30份增补合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        section_status={
            "_pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-16",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["审核30份增补合同"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去游泳"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="计划吧", source="test")

    assert result.report_saved is True
    assert "今天日报的“明日计划”" in result.message
    assert "计划去游泳" in result.message
    assert "今天的日报哪个部分" not in result.message
    pending = _assert_current_cursor(store, "tomorrow_plan")
    assert pending["context"]["focus_section"] == "tomorrow_plan"
    assert pending["context"]["stage"] == "awaiting_field_edit_content"


@pytest.mark.asyncio
async def test_current_report_edit_flow_persists_after_write(monkeypatch):
    current = _report(
        today_work=["审核30份增补合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        section_status={
            "_pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "tomorrow_plan",
                "context": {
                    "target_date": "2026-06-16",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": "tomorrow_plan",
                    "current_report": {
                        "today_work": ["审核30份增补合同"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去游泳"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="replace_field", field="tomorrow_plan", items=["计划去跑步"])],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="改成跑步", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["跑步"]
    pending = _assert_current_cursor(store, "tomorrow_plan")
    assert pending["context"]["current_report"]["tomorrow_plan"] == ["跑步"]
    assert pending["context"]["edit_cursor"]["active_draft_snapshot"]["tomorrow_plan"] == ["跑步"]


@pytest.mark.asyncio
async def test_current_report_edit_flow_replaces_full_report_from_prose(monkeypatch):
    current = _report(
        today_work=["审核30份增补合同", "发送20余份邮件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去跑步"],
        section_status={
            "_pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-16",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["审核30份增补合同", "发送20余份邮件"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去跑步"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    ask = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="出差去了南京开庭，同时审核了8份合同，写了10份函件，处理了工人讨薪。发现部分工人没签劳动合同，明天计划去苏州开庭。",
        source="test",
    )
    result = await service.submit_text(FakeSession(), user=_user(), raw_input="确认", source="test")

    assert ask.report_saved is True
    assert "这些操作需要确认" in ask.message
    assert result.report_saved is True
    assert service.report_agent.calls == []
    assert store["report"].today_work == ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"]
    assert store["report"].problems == ["发现部分工人未签劳动合同"]
    assert store["report"].tomorrow_plan == ["去苏州开庭"]
    pending = _assert_current_cursor(store, "tomorrow_plan")
    assert pending["context"]["current_report"]["today_work"] == ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"]
    assert "当前日报草稿" in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input",
    [
        "删除今日工作的第2条",
        "今日工作第2条删掉",
        "“今日工作第2条删掉”",
        "2. 更新函件管理办法，这个删除",
        "我准备吃午饭了，刚才今日工作的第二条我还没做好，删除",
    ],
)
async def test_current_report_edit_flow_delete_item_uses_cursor(monkeypatch, raw_input):
    current = _report(
        today_work=["处理部门费用", "更新函件管理办法"],
        problems=["暂无明显问题"],
        tomorrow_plan=["完善技能并上线第一稿"],
        section_status={
            "_pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-16",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["处理部门费用", "更新函件管理办法"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["完善技能并上线第一稿"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    ask = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test")

    assert ask.report_saved is True
    assert service.report_agent.calls == []
    assert "确认删除" not in ask.message
    assert "已删除今日工作第2条" in ask.message
    assert "更新函件管理办法" in ask.message
    assert "我刚才没理解" not in ask.message
    assert store["report"].today_work == ["处理部门费用"]
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "current_report_edit_flow"
    assert pending["operation"] == "modify_report"
    assert pending["target_field"] == "today_work"
    assert pending["context"]["current_report"]["today_work"] == ["处理部门费用"]


@pytest.mark.asyncio
async def test_current_report_edit_flow_delete_confirm_restores_cursor(monkeypatch):
    current = _report(
        today_work=["处理部门费用", "更新函件管理办法"],
        problems=["暂无明显问题"],
        tomorrow_plan=["完善技能并上线第一稿"],
        section_status={
            "_pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-16",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["处理部门费用", "更新函件管理办法"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["完善技能并上线第一稿"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    ask = await service.submit_text(FakeSession(), user=_user(), raw_input="删除今日工作的第2条", source="test")
    assert "确认删除" not in ask.message
    assert ask.report_saved is True
    assert store["report"].today_work == ["处理部门费用"]
    pending = _assert_current_cursor(store, "today_work")
    assert pending["context"]["current_report"]["today_work"] == ["处理部门费用"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input, branch_text",
    [
        ("草稿再发我下", "处理部门费用"),
        ("明日计划全删了", "当前日报草稿"),
        ("问题改成暂无", "没有实际改动草稿"),
        ("函件改成邮件", "当前日报草稿"),
        ("明日计划补充去苏州开庭", "当前日报草稿"),
        ("第2条改成更新制度流程", "当前日报草稿"),
    ],
)
async def test_current_report_edit_flow_short_operations_do_not_fall_through(monkeypatch, raw_input, branch_text):
    current = _report(
        today_work=["处理部门费用", "更新函件管理办法"],
        problems=["暂无明显问题"],
        tomorrow_plan=["完善技能并上线第一稿"],
        section_status={
            "_pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-16",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["处理部门费用", "更新函件管理办法"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["完善技能并上线第一稿"],
                    },
                },
            }
        },
    )
    _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test", report_date=date(2026, 6, 16))

    assert service.report_agent.calls == []
    assert branch_text in result.message
    assert "我刚才没理解" not in result.message


@pytest.mark.asyncio
async def test_historical_report_field_selection_keeps_focus(monkeypatch):
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["昨天审核合同A"],
                        "problems": ["昨天无明显问题"],
                        "tomorrow_plan": ["今天跟进合同B"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="问题", source="test")

    assert result.report_saved is True
    assert "2026-06-15 日报的“问题/风险”" in result.message
    assert "昨天无明显问题" in result.message
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "historical_report_edit_flow"
    assert pending["target_field"] == "problems"
    assert pending["context"]["focus_section"] == "problems"
    assert pending["context"]["stage"] == "awaiting_field_edit_content"


@pytest.mark.asyncio
async def test_historical_report_repeated_field_does_not_loop(monkeypatch):
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "problems",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": "problems",
                    "current_report": {
                        "today_work": ["昨天审核合同A"],
                        "problems": ["昨天无明显问题"],
                        "tomorrow_plan": ["今天跟进合同B"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="问题", source="test")

    assert result.report_saved is True
    assert "已经定位到 2026-06-15 日报的“问题/风险”" in result.message
    assert "请直接告诉我要怎么改" in result.message
    assert "补充还是修改" not in result.message
    assert store["report"].section_status["_pending_interaction"]["context"]["focus_section"] == "problems"


@pytest.mark.asyncio
async def test_historical_report_field_display_uses_target_date(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["昨天审核合同A"],
        problems=["昨天无明显问题"],
        tomorrow_plan=["今天跟进合同B"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "problems",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": "problems",
                    "current_report": {
                        "today_work": ["昨天审核合同A"],
                        "problems": ["昨天无明显问题"],
                        "tomorrow_plan": ["今天跟进合同B"],
                    },
                },
            }
        }
    )
    _install_store(monkeypatch, current, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="你先发我看看", source="test")

    assert result.report_saved is False
    assert "2026-06-15" in result.message
    assert "问题/风险" in result.message
    assert "昨天无明显问题" in result.message
    assert "昨天审核合同A" not in result.message


@pytest.mark.asyncio
async def test_historical_report_replace_requires_confirmation_then_updates_previous(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["昨天审核合同A"],
        problems=["昨天无明显问题"],
        tomorrow_plan=["今天跟进合同B"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "problems",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": "problems",
                    "current_report": {
                        "today_work": ["昨天审核合同A"],
                        "problems": ["昨天无明显问题"],
                        "tomorrow_plan": ["今天跟进合同B"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current, previous=previous)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 17, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="update_historical_report",
                        field="problems",
                        items=["暂无问题"],
                        target_date="2026-06-15",
                        requires_confirmation=True,
                    )
                ],
                reason="Replace historical problems section.",
            )
        ]
    )

    ask = await service.submit_text(FakeSession(), user=_user(), raw_input="改成暂无问题", source="test")

    assert ask.report_saved is True
    assert "确认" in ask.message
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["operation"] == "update_historical_report"

    done = await service.submit_text(FakeSession(), user=_user(), raw_input="确认", source="test")

    assert done.report_saved is True
    assert store["previous"].problems == ["暂无问题"]
    pending = _assert_historical_cursor(store, "problems")
    assert pending["context"]["current_report"]["problems"] == ["暂无问题"]
    assert "修改后的日报" in done.message
    assert "暂无问题" in done.message


@pytest.mark.asyncio
async def test_historical_report_edit_flow_replaces_full_report_from_prose_after_confirmation(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["审核30份增补合同", "发送20余份函件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        today_work=["今天原内容不能被覆盖"],
        problems=["今天问题不能被覆盖"],
        tomorrow_plan=["今天计划不能被覆盖"],
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["审核30份增补合同", "发送20余份函件"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去游泳"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, current, previous=previous)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 17, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    ask = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="出差去了南京开庭，同时审核了8份合同，写了10份函件，处理了工人讨薪。发现部分工人没签劳动合同，明天计划去苏州开庭。",
        source="test",
    )

    assert ask.report_saved is True
    assert service.report_agent.calls == []
    assert "确认用这段内容整体替换 2026-06-15 日报吗" in ask.message
    assert store["previous"].today_work == ["审核30份增补合同", "发送20余份函件"]
    assert store["report"].today_work == ["今天原内容不能被覆盖"]

    done = await service.submit_text(FakeSession(), user=_user(), raw_input="确认", source="test")

    assert done.report_saved is True
    assert store["previous"].today_work == ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"]
    assert store["previous"].problems == ["发现部分工人未签劳动合同"]
    assert store["previous"].tomorrow_plan == ["去苏州开庭"]
    assert store["report"].today_work == ["今天原内容不能被覆盖"]
    pending = _assert_historical_cursor(store, "none")
    assert pending["context"]["current_report"]["tomorrow_plan"] == ["去苏州开庭"]
    assert "已整体替换 2026-06-15 日报" in done.message


@pytest.mark.asyncio
async def test_historical_report_direct_edit_instruction_after_entry_uses_agent_context(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["昨天审核合同A"],
        problems=["昨天无明显问题"],
        tomorrow_plan=["今天跟进合同B"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["昨天审核合同A"],
                        "problems": ["昨天无明显问题"],
                        "tomorrow_plan": ["今天跟进合同B"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="update_historical_report",
                        field="problems",
                        items=["暂无问题"],
                        target_date="2026-06-15",
                        requires_confirmation=True,
                    )
                ],
                reason="The user directly edits a field in the pending historical report context.",
            )
        ]
    )

    ask = await service.submit_text(FakeSession(), user=_user(), raw_input="把昨天无明显问题改成暂无问题", source="test")

    assert service.report_agent.calls == []
    assert "确认" in ask.message
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["operation"] == "update_historical_report"

    done = await service.submit_text(FakeSession(), user=_user(), raw_input="确认", source="test")

    assert done.report_saved is True
    assert store["previous"].problems == ["暂无问题"]
    pending = _assert_historical_cursor(store, "problems")
    assert pending["context"]["current_report"]["problems"] == ["暂无问题"]


@pytest.mark.asyncio
async def test_historical_report_short_numbered_delete_uses_saved_context(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["审核30份增补合同", "发送20余份函件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["审核30份增补合同", "发送20余份函件"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去游泳"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="delete_item",
                        item_indices=[1],
                        requires_confirmation=True,
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="第一条删了", source="test")

    assert result.report_saved is True
    assert "历史日报不能删除" in result.message
    assert store["previous"].today_work == ["审核30份增补合同", "发送20余份函件"]
    assert store["previous"].tomorrow_plan == ["计划去游泳"]


@pytest.mark.asyncio
async def test_historical_report_short_text_patch_uses_saved_context(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["审核30份增补合同", "发送20余份函件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["审核30份增补合同", "发送20余份函件"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去游泳"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="replace_text",
                        old_value="函件",
                        new_value="邮件",
                        requires_confirmation=True,
                    )
                ],
            )
        ]
    )

    ask = await service.submit_text(FakeSession(), user=_user(), raw_input="函件改成邮件", source="test")

    assert ask.report_saved is True
    assert "确认" in ask.message
    assert "函件" in ask.message
    assert "邮件" in ask.message
    assert store["previous"].today_work == ["审核30份增补合同", "发送20余份函件"]

    done = await service.submit_text(FakeSession(), user=_user(), raw_input="确认", source="test")

    assert done.report_saved is True
    assert store["previous"].today_work == ["审核30份增补合同", "发送20余份邮件"]
    assert store["previous"].tomorrow_plan == ["计划去游泳"]
    pending = _assert_historical_cursor(store, "today_work")
    assert pending["context"]["current_report"]["today_work"] == ["审核30份增补合同", "发送20余份邮件"]


@pytest.mark.asyncio
async def test_historical_report_clear_focused_section_from_short_reply(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["审核30份增补合同", "发送20余份函件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "tomorrow_plan",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": "tomorrow_plan",
                    "current_report": {
                        "today_work": ["审核30份增补合同", "发送20余份函件"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去游泳"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="clear_field",
                        field="tomorrow_plan",
                        requires_confirmation=True,
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="全删了", source="test")

    assert result.report_saved is True
    assert "历史日报不能删除" in result.message
    assert store["previous"].tomorrow_plan == ["计划去游泳"]
    assert store["previous"].today_work == ["审核30份增补合同", "发送20余份函件"]


@pytest.mark.asyncio
async def test_historical_report_focused_cursor_rejects_conflicting_llm_field(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["审核30份增补合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "tomorrow_plan",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": "tomorrow_plan",
                    "current_report": {
                        "today_work": ["审核30份增补合同"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去游泳"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="replace_field", field="today_work", items=["错误改到今日工作"])],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="改成跑步", source="test")

    assert result.report_saved is True
    assert "确认把 2026-06-15 日报的“明日计划”改成“跑步”" in result.message
    assert store["previous"].today_work == ["审核30份增补合同"]
    assert store["previous"].tomorrow_plan == ["计划去游泳"]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_historical_report_cursor_rejects_llm_target_date_drift(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["审核30份增补合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_COMPLETED,
    )
    current = _report(
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "tomorrow_plan",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": "tomorrow_plan",
                    "current_report": {
                        "today_work": ["审核30份增补合同"],
                        "problems": ["暂无明显问题"],
                        "tomorrow_plan": ["计划去游泳"],
                    },
                },
            }
        }
    )
    store = _install_store(monkeypatch, current, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="replace_field", items=["计划去跑步"], target_date="2026-06-16")],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="改成跑步", source="test")

    assert result.report_saved is True
    assert "确认把 2026-06-15 日报的“明日计划”改成“跑步”" in result.message
    assert store["previous"].tomorrow_plan == ["计划去游泳"]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_history_query_then_followup_edit_stays_on_yesterday(monkeypatch):
    current = _report(
        today_work=["审核30份增补合同", "发送20余份邮件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_PENDING_CONFIRMATION,
    )
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["审核30份增补合同", "发送20余份函件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        status=STATUS_COMPLETED,
    )
    store = _install_store(monkeypatch, current, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="query_history",
                confidence="high",
                should_write=False,
                actions=[AgentAction(type="query_history", target_date="yesterday")],
            ),
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="replace_field", field="tomorrow_plan", items=["计划去跑步"])],
            ),
        ]
    )

    query = await service.submit_text(FakeSession(), user=_user(), raw_input="再发我下昨天的日报", source="test")
    assert "2026-06-15" in query.message
    assert store["report"].section_status["_pending_interaction"]["type"] == "historical_report_edit_flow"

    entry = await service.submit_text(FakeSession(), user=_user(), raw_input="昨天的改下", source="test")
    assert "我先把昨天的日报调出来" in entry.message
    assert store["report"].section_status["_pending_interaction"]["context"]["target_date"] == "2026-06-15"

    focus = await service.submit_text(FakeSession(), user=_user(), raw_input="计划把", source="test")
    assert "2026-06-15" in focus.message
    assert "明日计划" in focus.message
    assert store["report"].section_status["_pending_interaction"]["context"]["focus_section"] == "tomorrow_plan"

    ask = await service.submit_text(FakeSession(), user=_user(), raw_input="把计划改成跑步", source="test")
    assert "确认" in ask.message
    assert store["previous"].tomorrow_plan == ["计划去游泳"]
    assert store["report"].tomorrow_plan == ["计划去游泳"]

    done = await service.submit_text(FakeSession(), user=_user(), raw_input="确认", source="test")
    assert done.report_saved is True
    assert store["previous"].tomorrow_plan == ["跑步"]
    assert store["report"].tomorrow_plan == ["计划去游泳"]
    pending = _assert_historical_cursor(store, "tomorrow_plan")
    assert pending["context"]["current_report"]["tomorrow_plan"] == ["跑步"]


@pytest.mark.asyncio
async def test_historical_report_flow_allows_switching_to_current_report(monkeypatch):
    current = _report(
        today_work=["今天审核合同"],
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["昨天审核合同A"],
                        "problems": ["昨天无明显问题"],
                        "tomorrow_plan": ["今天跟进合同B"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="replace_field", field="today_work", items=["今天审核20份合同"])],
                reason="User explicitly switched to current report.",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="改今天的日报，今日工作改成今天审核20份合同", source="test")

    assert service.report_agent.calls
    assert result.report_saved is True
    assert store["report"].today_work == ["今天审核20份合同"]
    pending = store["report"].section_status.get("_pending_interaction")
    assert pending is None or pending["type"] != "awaiting_action_confirmation"


@pytest.mark.asyncio
async def test_historical_report_flow_allows_today_work_without_report_keyword(monkeypatch):
    current = _report(
        today_work=["今天审核合同"],
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-15",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["昨天审核合同A"],
                        "problems": ["昨天无明显问题"],
                        "tomorrow_plan": ["今天跟进合同B"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, current)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="append_items", field="today_work", items=["处理一个小合同"])],
                reason="User explicitly recorded today's work.",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="今天处理了一个小合同", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["今天审核合同", "处理一个小合同"]
    pending = store["report"].section_status.get("_pending_interaction")
    assert pending is None or pending["type"] != "awaiting_action_confirmation"


@pytest.mark.asyncio
async def test_no_write_replace_text_action_is_executed_as_safe_patch(monkeypatch):
    existing = _report(tomorrow_plan=["明天我去有用"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                actions=[
                    AgentAction(
                        type="replace_text",
                        field="tomorrow_plan",
                        target_item_index=1,
                        old_value="明天我去有用",
                        new_value="明天我去游泳",
                    )
                ],
                reply_to_user="已为您将明日计划中的“有用”改为“游泳”。",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="不是，我其实想说游泳", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["明天我去游泳"]
    pending = store["report"].section_status.get("_pending_interaction")
    assert pending is None or pending["type"] != "awaiting_action_confirmation"
    assert "当前日报草稿" in result.message


@pytest.mark.asyncio
async def test_replace_text_field_is_redirected_to_unique_matching_item(monkeypatch):
    existing = _report(today_work=["审核30份合同", "发送20份函件"], tomorrow_plan=["去游泳"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="replace_text",
                        field="tomorrow_plan",
                        old_value="合同",
                        new_value="增补合同",
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="合同改成增补合同", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["审核30份增补合同", "发送20份函件"]
    assert store["report"].tomorrow_plan == ["去游泳"]


@pytest.mark.asyncio
async def test_no_write_previous_plan_action_is_executed(monkeypatch):
    existing = _report(
        section_status={
            "_reference_report_context": {
                "source": "pasted_previous_report",
                "today_work": ["审核合同A"],
                "problems": ["暂无"],
                "tomorrow_plan": ["跟进合同B", "整理案件C材料"],
            }
        }
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                actions=[
                    AgentAction(
                        type="complete_previous_plan_item",
                        source_field="tomorrow_plan",
                        target_field="today_work",
                        item_indices=[1],
                        source_item_text="跟进合同B",
                    )
                ],
                reply_to_user="已确认“跟进合同B”完成，已移至今日工作。",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="其中第1项已完成", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["完成跟进合同B"]
    assert "当前日报草稿" in result.message


@pytest.mark.asyncio
async def test_ambiguous_section_reply_does_not_write(monkeypatch):
    existing = _report(section_status={"_pending_interaction": {"type": "awaiting_append_target", "operation": "append"}})
    store = _install_store(monkeypatch, existing)
    plan = ActionPlan(
        intent="edit_draft",
        confidence="medium",
        should_write=False,
        reply_to_user="您想补充到哪个部分？今日工作、问题还是明日计划？",
        pending_interaction_to_set=PendingInteractionPlan(type="awaiting_append_target", operation="append", target_field="none"),
    )
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([plan])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="额", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == []
    assert store["report"].problems == []
    assert store["report"].tomorrow_plan == []
    assert "哪个部分" in result.message


@pytest.mark.asyncio
async def test_update_then_query_reads_same_saved_report(monkeypatch):
    existing = _report(section_status={"tomorrow_plan_acknowledged_empty": True})
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(intent="query_current", confidence="high", should_write=False, actions=[]),
        ]
    )

    first = await service.submit_text(FakeSession(), user=_user(), raw_input="明日计划 我明天计划去游泳", source="test")
    second = await service.submit_text(FakeSession(), user=_user(), raw_input="发我看下", source="test")

    assert first.report_saved is True
    assert store["report"].tomorrow_plan == ["去游泳"]
    assert "去游泳" in second.message
    assert "无明日计划" not in second.message


@pytest.mark.asyncio
async def test_no_plan_placeholder_is_not_stored_and_replaced_by_real_plan(monkeypatch):
    existing = _report(tomorrow_plan=["无明日计划"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="append_items", field="tomorrow_plan", items=["计划去游泳"])],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="明天我去游泳", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["去游泳"]


@pytest.mark.asyncio
async def test_write_report_entry_does_not_default_to_append_flow(monkeypatch):
    store = _install_store(monkeypatch, None)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="unclear",
                confidence="medium",
                should_write=False,
                reply_to_user="可以，请告诉我今天主要做了什么、有没有问题或风险，以及明天计划。",
                actions=[AgentAction(type="ask_clarification")],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="帮我写日报吧", source="test")

    assert result.report_saved is False
    assert store["report"] is None
    assert "今天主要做了什么" in result.message


@pytest.mark.asyncio
async def test_write_report_entry_ignores_agent_append_pending_suggestion(monkeypatch):
    existing = _report()
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=False,
                reply_to_user="您想补充到哪个部分？今日工作、问题还是明日计划？",
                pending_interaction_to_set=PendingInteractionPlan(type="awaiting_append_target", operation="append", target_field="none"),
                actions=[AgentAction(type="ask_clarification")],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="帮我写日报吧", source="test")

    assert result.report_saved is False
    pending = store["report"].section_status.get("_pending_interaction")
    assert pending is None or pending["type"] != "awaiting_action_confirmation"
    assert "今天主要做了什么" in result.message
    assert "补充到哪个部分" not in result.message


@pytest.mark.asyncio
async def test_delete_confirmation_need_deletes_pending_item(monkeypatch):
    existing = _report(
        today_work=["第一条修复技能与第五条恢复 work body 技能合并为一件事", "发送20余份函件"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [1]},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="需要", source="test")

    assert result.report_saved is True
    assert "当前日报草稿" in result.message
    assert "发送20余份函件" in result.message
    assert store["report"].today_work == ["发送20余份函件"]
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_delete_confirmation_cancel_keeps_item_and_clears_pending(monkeypatch):
    existing = _report(
        today_work=["第一条", "第二条"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [1]},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="不用了", source="test")

    assert result.report_saved is True
    assert "取消删除" in result.message
    assert store["report"].today_work == ["第一条", "第二条"]
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_batch_delete_uses_original_indices(monkeypatch):
    existing = _report(
        today_work=["第一条", "第二条", "第三条"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [1, 2]},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="确认", source="test")

    assert result.report_saved is True
    assert "当前日报草稿" in result.message
    assert "第三条" in result.message
    assert store["report"].today_work == ["第三条"]


@pytest.mark.asyncio
async def test_delete_then_query_reads_latest_database_content(monkeypatch):
    existing = _report(
        today_work=["第一条", "第二条"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [1]},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([ActionPlan(intent="query_current", confidence="high", should_write=False, actions=[])])

    delete_result = await service.submit_text(FakeSession(), user=_user(), raw_input="需要", source="test")
    query_result = await service.submit_text(FakeSession(), user=_user(), raw_input="展示给我看看", source="test")

    assert delete_result.report_saved is True
    assert store["report"].today_work == ["第二条"]
    assert "第二条" in query_result.message
    assert "第一条" not in query_result.message


@pytest.mark.asyncio
async def test_agent_delete_confirmation_state_is_persisted(monkeypatch):
    existing = _report(today_work=["第一条", "第二条"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                reply_to_user="您是说今日工作的第1条“第一条”需要删除吗？请确认。",
                pending_interaction_to_set=PendingInteractionPlan(
                    type="awaiting_action_confirmation",
                    operation="delete_report_item",
                    target_field="today_work",
                    context={"item_indices": [1]},
                ),
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="删除今日工作的1", source="test")

    assert result.report_saved is True
    assert "请确认" in result.message
    assert store["report"].today_work == ["第一条", "第二条"]
    assert store["report"].section_status["_pending_interaction"]["operation"] == "delete_report_item"
    assert store["report"].section_status["_pending_interaction"]["context"]["item_indices"] == [1]


@pytest.mark.asyncio
async def test_natural_language_delete_saves_pending_then_confirm_executes(monkeypatch):
    existing = _report(status=STATUS_COMPLETED, today_work=["审核30份增补合同"], problems=[], tomorrow_plan=["计划去游泳"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                reply_to_user="确认删除今日工作里的“审核30份增补合同”吗？",
                pending_interaction_to_set=PendingInteractionPlan(
                    type="awaiting_action_confirmation",
                    operation="delete_report_item",
                    target_field="today_work",
                    context={
                        "item_indices": [1],
                        "action": {"type": "delete_item", "field": "today_work", "item_indices": [1]},
                    },
                ),
            )
        ]
    )

    ask = await service.submit_text(FakeSession(), user=_user(), raw_input="30份合同删掉", source="test")
    done = await service.submit_text(FakeSession(), user=_user(), raw_input="\u5bf9", source="test")

    assert ask.report_saved is True
    assert store["report"].today_work == []
    assert done.report_saved is True
    assert done.status == STATUS_COMPLETED
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_numbered_delete_action_executes_without_confirmation(monkeypatch):
    existing = _report(status=STATUS_COMPLETED, today_work=["审核30份增补合同", "发送20份函件"], problems=[], tomorrow_plan=["计划去游泳"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="delete_item",
                        field="today_work",
                        item_indices=[1],
                        requires_confirmation=True,
                        confirmation_message="确认删除今日工作第1条“审核30份增补合同”吗？",
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="今日工作的1，帮我删除", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert "是否撤回并修改" not in result.message
    assert "确认删除" not in result.message
    assert store["report"].status == STATUS_COMPLETED
    assert store["report"].today_work == ["发送20份函件"]
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_pending_confirmation_confirm_does_not_submit_report(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["审核30份增补合同"],
        problems=[],
        tomorrow_plan=["计划去游泳"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [1], "action": {"type": "delete_item", "field": "today_work", "item_indices": [1]}},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u786e\u8ba4",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.reply_kind == "agent_report_update"
    assert store["report"].today_work == []
    assert store["report"].status == STATUS_COMPLETED


@pytest.mark.asyncio
async def test_completed_delete_pending_accepts_en_without_reconfirming(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"],
        problems=["发现部分工人未签劳动合同"],
        tomorrow_plan=["去苏州开庭"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="delete_item",
                        field="today_work",
                        item_indices=[3],
                        requires_confirmation=True,
                        confirmation_message="确认删除今日工作第3条“撰写10份函件”吗？",
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="第三条删掉吧", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert "是否撤回并修改" not in result.message
    assert "确认删除" not in result.message
    assert "撰写10份函件" not in store["report"].today_work
    assert store["report"].status == STATUS_COMPLETED
    pending = store["report"].section_status.get("_pending_interaction")
    assert pending is None or pending["type"] != "awaiting_action_confirmation"


@pytest.mark.asyncio
async def test_noisy_confirmation_executes_pending_delete_once(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"],
        problems=["发现部分工人未签劳动合同"],
        tomorrow_plan=["去苏州开庭"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [3], "action": {"type": "delete_item", "field": "today_work", "item_indices": [3]}},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="读哦\n对", source="test")

    assert result.report_saved is True
    assert "没理解" not in result.message
    assert "撰写10份函件" not in store["report"].today_work
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_restore_recent_deleted_item_from_snapshot(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同", "处理工人讨薪"],
        problems=["发现部分工人未签劳动合同"],
        tomorrow_plan=["去苏州开庭"],
        section_status={
            "_previous_draft_snapshot": {
                "today_work": ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"],
                "problems": ["发现部分工人未签劳动合同"],
                "tomorrow_plan": ["去苏州开庭"],
                "status": STATUS_COMPLETED,
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="算了不删了 加回去吧",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert store["report"].today_work == ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"]
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_restore_recent_deleted_item_with_wrong_prefix(monkeypatch):
    existing = _report(
        status=STATUS_PENDING_CONFIRMATION,
        today_work=["处理部门费用", "整理归档材料", "沟通服务器方案", "完善日报系统"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天继续推进"],
        section_status={
            "_previous_draft_snapshot": {
                "today_work": ["处理部门费用", "更新函件管理办法", "整理归档材料", "沟通服务器方案", "完善日报系统"],
                "problems": ["暂无明显问题"],
                "tomorrow_plan": ["明天继续推进"],
                "status": STATUS_PENDING_CONFIRMATION,
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="不对，加回来",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert "更新函件管理办法" in store["report"].today_work
    assert store["report"].today_work[1] == "更新函件管理办法"


@pytest.mark.asyncio
async def test_bare_withdraw_restores_previous_snapshot_when_recent_edit_exists(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同", "处理工人讨薪"],
        problems=["发现部分工人未签劳动合同"],
        tomorrow_plan=["去苏州开庭"],
        section_status={
            "_previous_draft_snapshot": {
                "today_work": ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"],
                "problems": ["发现部分工人未签劳动合同"],
                "tomorrow_plan": ["去苏州开庭"],
                "status": STATUS_COMPLETED,
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="撤回", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert store["report"].status == STATUS_COMPLETED
    assert store["report"].today_work == ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"]
    assert "已撤回日报" not in result.message


@pytest.mark.asyncio
async def test_delete_recently_restored_item_without_asking_target(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"],
        problems=["发现部分工人未签劳动合同"],
        tomorrow_plan=["去苏州开庭"],
        section_status={
            "_previous_draft_snapshot": {
                "today_work": ["出差去南京开庭", "审核8份合同", "处理工人讨薪"],
                "problems": ["发现部分工人未签劳动合同"],
                "tomorrow_plan": ["去苏州开庭"],
                "status": STATUS_COMPLETED,
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="算了还是删掉吧", source="test")

    assert result.report_saved is True
    assert "删除哪" not in result.message
    assert store["report"].today_work == ["出差去南京开庭", "审核8份合同", "处理工人讨薪"]
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_text_reference_uses_recent_item_context(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"],
        problems=["发现部分工人未签劳动合同"],
        tomorrow_plan=["去苏州开庭"],
        section_status={
            "_previous_draft_snapshot": {
                "today_work": ["出差去南京开庭", "审核8份合同", "处理工人讨薪"],
                "problems": ["发现部分工人未签劳动合同"],
                "tomorrow_plan": ["去苏州开庭"],
                "status": STATUS_COMPLETED,
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="函件那条", source="test")

    assert result.report_saved is True
    assert "删除哪" not in result.message
    assert store["report"].today_work == ["出差去南京开庭", "审核8份合同", "处理工人讨薪"]


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", ["撤回", "确认"])
async def test_unsubmit_report_confirmation_executes_and_clears_pending(monkeypatch, confirmation):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["去苏州开庭"],
        section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="可以给我撤回提交的日报么", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert "确认撤回已提交日报吗" not in result.message
    assert store["report"].status == STATUS_COLLECTING
    assert store["report"].confirmation_type == CONFIRMATION_NONE
    assert store["report"].confirmed_by_user is False
    assert "_pending_interaction" not in store["report"].section_status
    assert "还在等待" not in result.message


@pytest.mark.asyncio
async def test_bare_withdraw_completed_report_without_snapshot_unsubmits(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["去苏州开庭"],
        section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="撤回", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert store["report"].status == STATUS_COLLECTING
    assert "已撤回日报" in result.message


@pytest.mark.asyncio
async def test_unsubmit_request_overrides_existing_delete_pending(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同", "撰写10份函件"],
        problems=["暂无明显问题"],
        tomorrow_plan=["去苏州开庭"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [3], "action": {"type": "delete_item", "field": "today_work", "item_indices": [3]}},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="可以给我撤回提交的日报么", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert "确认撤回已提交日报吗" not in result.message
    assert store["report"].today_work == ["出差去南京开庭", "审核8份合同", "撰写10份函件"]
    assert store["report"].status == STATUS_COLLECTING
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_confirm_without_pending_can_submit_report(monkeypatch):
    existing = _report(
        status=STATUS_PENDING_CONFIRMATION,
        today_work=["审核30份合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["计划去游泳"],
        section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="confirm_submit",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="submit_report")],
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u786e\u8ba4",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert store["report"].status == STATUS_COMPLETED
    assert store["report"].confirmed_by_user is True


@pytest.mark.asyncio
async def test_confirm_submit_defaults_missing_problem_to_no_problem(monkeypatch):
    existing = _report(
        status=STATUS_COLLECTING,
        today_work=["日常用印资料审核", "跟进来函处理进展"],
        problems=[],
        tomorrow_plan=["来函进展跟进"],
        section_status={"today_work": True, "problems": False, "tomorrow_plan": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="确认提交", source="test")

    assert result.report_saved is True
    assert result.reply_kind == "confirmed"
    assert result.status == STATUS_COMPLETED
    assert store["report"].problems == ["暂无明显问题"]
    assert store["report"].section_status["problems"] is True
    assert store["report"].section_status["problems_acknowledged_empty"] is True


@pytest.mark.asyncio
async def test_executor_failure_keeps_pending_action(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["第一条", "第二条"],
        problems=[],
        tomorrow_plan=["计划去游泳"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [3], "action": {"type": "delete_item", "field": "today_work", "item_indices": [3]}},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u5bf9", source="test")

    assert result.report_saved is False
    assert store["report"].today_work == ["第一条", "第二条"]
    assert store["report"].section_status["_pending_interaction"]["operation"] == "delete_report_item"
    assert result.status == STATUS_COMPLETED


@pytest.mark.asyncio
async def test_zero_based_agent_index_is_normalized_and_warned(monkeypatch, caplog):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["审核30份增补合同"],
        problems=[],
        tomorrow_plan=["计划去游泳"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [0], "action": {"type": "delete_item", "field": "today_work", "item_indices": [0]}},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    with caplog.at_level(logging.WARNING, logger="ai_review_agent_executor"):
        result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u5bf9", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == []
    assert any("agent_index_normalized" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_add_report_content_does_not_create_pending_confirmation(monkeypatch):
    existing = _report()
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="append_items", field="today_work", items=["审核18份合同", "发送20份函件"])],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="今天审核18份合同，发了20份函件", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["审核18份合同", "发送20份函件"]
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_light_confirmation_consumed_once_without_submit(monkeypatch):
    existing = _report(
        section_status={
            "_pending_interaction": {
                "type": "awaiting_append_target_confirmation",
                "operation": "append",
                "target_field": "problems",
            }
        }
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u5bf9", source="test")

    assert result.report_saved is True
    assert store["report"].status == STATUS_COLLECTING
    assert store["report"].section_status["_pending_interaction"]["type"] == "awaiting_append_content"
    assert store["report"].section_status["_pending_interaction"]["target_field"] == "problems"


@pytest.mark.asyncio
async def test_load_reference_report_does_not_copy_yesterday_work(monkeypatch):
    store = _install_store(monkeypatch, None)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="load_reference_report",
                        source="pasted_previous_report",
                        reference_report={
                            "report_date": "2026-06-15",
                            "today_work": ["审核合同A"],
                            "problems": ["暂无明显问题"],
                            "tomorrow_plan": ["跟进合同B", "整理案件C材料"],
                        },
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="昨日日报如下", source="test")

    assert result.report_saved is True
    assert "昨天日报参考" in result.message
    assert store["report"].today_work == []
    assert store["report"].tomorrow_plan == []
    reference = store["report"].section_status["_reference_report_context"]
    assert reference["tomorrow_plan"] == ["跟进合同B", "整理案件C材料"]


@pytest.mark.asyncio
async def test_pasted_yesterday_report_is_intercepted_before_agent(monkeypatch):
    store = _install_store(monkeypatch, None)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="昨日日报：\n今日工作：\n1. 审核合同A\n\n问题/风险：\n暂无\n\n明日计划：\n1. 跟进合同B\n2. 整理案件C材料",
        source="test",
    )

    assert result.report_saved is True
    assert "昨天日报参考" in result.message
    assert store["report"].today_work == []
    assert store["report"].problems == []
    assert store["report"].tomorrow_plan == []
    assert store["report"].section_status["_reference_report_context"]["today_work"] == ["审核合同A"]
    assert store["report"].section_status["_reference_report_context"]["tomorrow_plan"] == ["跟进合同B", "整理案件C材料"]


@pytest.mark.asyncio
async def test_pasted_dated_report_template_is_reference_not_current(monkeypatch):
    current_date = date(2026, 6, 26)
    current = _report(report_date=current_date, today_work=[], problems=[], tomorrow_plan=[])
    store = _install_multiday_store(monkeypatch, {current_date: current})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="当前填报日期：2026-06-25\n今日工作：过去今日工作\n问题/风险：暂无\n明日计划：1. 日常用印流程审批\n2. 现场用印审核\n3. 电子章用印",
        source="test",
    )

    assert result.report_saved is True
    assert store[current_date].today_work == []
    assert store[current_date].problems == []
    assert store[current_date].tomorrow_plan == []
    reference = store[current_date].section_status["_reference_report_context"]
    assert reference["today_work"] == ["过去今日工作"]
    assert reference["tomorrow_plan"] == ["日常用印流程审批", "现场用印审核", "电子章用印"]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_complete_reference_plan_item_after_pasted_yesterday_report(monkeypatch):
    existing = _report(
        section_status={
            "_reference_report_context": {
                "source": "pasted_previous_report",
                "report_date": "2026-06-15",
                "today_work": ["审核合同A"],
                "problems": ["暂无明显问题"],
                "tomorrow_plan": ["跟进合同B", "整理案件C材料"],
            }
        }
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="complete_previous_plan_item", item_indices=[1])],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="其中第1项已完成", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["完成跟进合同B"]
    assert store["report"].tomorrow_plan == []
    assert "当前日报草稿" in result.message
    assert "完成跟进合同B" in result.message


@pytest.mark.asyncio
async def test_complete_all_previous_plan_items_from_database_yesterday(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["昨天今日工作不能复制"],
        tomorrow_plan=["跟进合同B", "整理案件C材料", "发送催告函D"],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="complete_all_previous_plan_items")],
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="昨天的待办都完成了",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert store["report"].today_work == ["完成跟进合同B", "完成整理案件C材料", "完成发送催告函D"]
    assert "昨天今日工作不能复制" not in store["report"].today_work
    assert "当前日报草稿" in result.message


@pytest.mark.asyncio
async def test_previous_plan_completion_reuses_plan_for_tomorrow_and_extracts_problem(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=[
            "日常用印流程审批",
            "现场用印审核",
            "电子章用印",
            "未归档合同催收",
            "用印值班半小时",
        ],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="今天完成了昨天的计划，然后明天的计划照着昨天的计划来。今日苏建院借章流程走到张新红总被退回，引申出体外公司的借章流程问题。",
        source="test",
    )

    assert result.report_saved is True
    assert service.report_agent.calls == []
    assert store["report"].today_work == [
        "完成日常用印流程审批",
        "完成现场用印审核",
        "完成电子章用印",
        "完成未归档合同催收",
        "完成用印值班半小时",
    ]
    assert store["report"].problems == ["苏建院借章流程走到张新红总被退回，引申出体外公司的借章流程问题"]
    assert store["report"].tomorrow_plan == [
        "日常用印流程审批",
        "现场用印审核",
        "电子章用印",
        "未归档合同催收",
        "用印值班半小时",
    ]
    assert all("苏建院" not in item and "问题" not in item for item in store["report"].tomorrow_plan)


@pytest.mark.asyncio
async def test_previous_plan_completion_strips_future_plan_shell(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=["明天继续完善mcp并开始测试", "明天开始做周报agent的outbox"],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="昨天的计划全部完成", source="test")

    assert result.report_saved is True
    assert service.report_agent.calls == []
    assert store["report"].today_work == ["完成完善mcp并开始测试", "完成周报agent的outbox"]
    assert "完成明天" not in "\n".join(store["report"].today_work)
    assert store["report"].tomorrow_plan == []


@pytest.mark.asyncio
async def test_rollover_first_previous_plan_item_and_keep_rest_for_tomorrow(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=["跟进合同B", "整理案件C材料", "发送催告函D"],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="rollover_previous_plan_items", item_indices=[1])],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="第1项完成了，剩下的明天继续", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["完成跟进合同B"]
    assert store["report"].tomorrow_plan == ["整理案件C材料", "发送催告函D"]
    assert "当前日报草稿" in result.message


@pytest.mark.asyncio
async def test_complete_previous_plan_item_by_text_match(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=["跟进合同B", "整理案件C材料", "发送催告函D"],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="complete_previous_plan_item", source_item_text="整理案件C材料")],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="整理案件C材料已经完成", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["完成整理案件C材料"]
    assert "当前日报草稿" in result.message


@pytest.mark.asyncio
async def test_previous_plan_range_reference_expands_items_and_drops_placeholders(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=[
            "\u8ddf\u8fdbA\u5408\u540c",
            "\u5ba1\u6838B\u5408\u540c",
            "\u6574\u7406C\u6750\u6599",
            "\u53d1\u9001D\u51fd\u4ef6",
            "\u5f52\u6863E\u6750\u6599",
            "\u529e\u7406F\u624b\u7eed",
        ],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(type="append_items", field="today_work", items=["\u5b8c\u6210\u6628\u65e5\u8ba1\u5212\u7b2c1~5\u9879", "\u7b2c6\u9879\u672a\u5b8c\u6210"]),
                    AgentAction(type="replace_field", field="problems", items=["\u6682\u65e0\u660e\u663e\u95ee\u9898"]),
                    AgentAction(type="replace_field", field="tomorrow_plan", items=["\u7ee7\u7eed\u5b8c\u6210\u6628\u65e5\u8ba1\u5212\u7b2c1~5\u9879"]),
                ],
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input=(
            "\u6628\u5929\u9664\u4e86\u7b2c6\u9879\u6ca1\u6709\u505a\u4e4b\u5916\uff0c\u5176\u4ed6\u6b63\u5e38\u5b8c\u6210\u3002\n"
            "\u4eca\u65e5\u5de5\u4f5c\u8ba1\u5212\u540c\u6628\u65e5\u8ba1\u5212\u76841~5\u9879\u3002\n"
            "\u6628\u65e5\u672a\u78b0\u5230\u65e0\u6cd5\u89e3\u51b3\u7684\u95ee\u9898\u3002"
        ),
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert store["report"].today_work == [
        "\u5b8c\u6210\u8ddf\u8fdbA\u5408\u540c",
        "\u5b8c\u6210\u5ba1\u6838B\u5408\u540c",
        "\u5b8c\u6210\u6574\u7406C\u6750\u6599",
        "\u5b8c\u6210\u53d1\u9001D\u51fd\u4ef6",
        "\u5b8c\u6210\u5f52\u6863E\u6750\u6599",
    ]
    assert store["report"].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert store["report"].tomorrow_plan == []
    joined = "\n".join(store["report"].today_work + store["report"].tomorrow_plan)
    assert "\u6628\u65e5\u8ba1\u5212" not in joined
    assert "\u7b2c6\u9879\u672a\u5b8c\u6210" not in joined


@pytest.mark.asyncio
async def test_previous_plan_range_reference_repairs_incomplete_replace_field(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=[
            "\u65e5\u5e38\u7528\u5370\u6d41\u7a0b\u5ba1\u6279",
            "\u73b0\u573a\u7528\u5370\u5ba1\u6838",
            "\u7535\u5b50\u7ae0\u7528\u5370",
            "\u672a\u5f52\u6863\u5408\u540c\u50ac\u6536",
            "\u7528\u5370\u4e8b\u5b9c\u54a8\u8be2\u7b54\u590d",
            "\u5408\u540c\u5f52\u6863\u6574\u7406\u79fb\u4ea4\u8d44\u6599\u5ba4",
        ],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="replace_field",
                        field="today_work",
                        items=[
                            "\u5b8c\u6210\u65e5\u5e38\u7528\u5370\u6d41\u7a0b\u5ba1\u6279",
                            "\u5b8c\u6210\u73b0\u573a\u7528\u5370\u5ba1\u6838",
                            "\u5b8c\u6210\u7535\u5b50\u7ae0\u7528\u5370",
                            "\u5b8c\u6210\u672a\u5f52\u6863\u5408\u540c\u50ac\u6536",
                        ],
                    ),
                    AgentAction(type="replace_field", field="problems", items=["\u6682\u65e0\u660e\u663e\u95ee\u9898"]),
                ],
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input=(
            "\u6628\u5929\u9664\u4e86\u7b2c6\u9879\u6ca1\u6709\u505a\u4e4b\u5916\uff0c\u5176\u4ed6\u6b63\u5e38\u5b8c\u6210\u3002"
            "\u4eca\u65e5\u5de5\u4f5c\u8ba1\u5212\u540c\u6628\u65e5\u8ba1\u5212\u76841~5\u9879\u3002"
            "\u6628\u65e5\u672a\u78b0\u5230\u65e0\u6cd5\u89e3\u51b3\u7684\u95ee\u9898\u3002"
        ),
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert store["report"].today_work == [
        "\u5b8c\u6210\u65e5\u5e38\u7528\u5370\u6d41\u7a0b\u5ba1\u6279",
        "\u5b8c\u6210\u73b0\u573a\u7528\u5370\u5ba1\u6838",
        "\u5b8c\u6210\u7535\u5b50\u7ae0\u7528\u5370",
        "\u5b8c\u6210\u672a\u5f52\u6863\u5408\u540c\u50ac\u6536",
        "\u5b8c\u6210\u7528\u5370\u4e8b\u5b9c\u54a8\u8be2\u7b54\u590d",
    ]


@pytest.mark.asyncio
async def test_previous_plan_except_item_completion_expands_remaining_items(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=[
            "\u65e5\u5e38\u7528\u5370\u6d41\u7a0b\u5ba1\u6279",
            "\u73b0\u573a\u7528\u5370\u5ba1\u6838",
            "\u7535\u5b50\u7ae0\u7528\u5370",
            "\u672a\u5f52\u6863\u5408\u540c\u50ac\u6536",
            "\u7528\u5370\u4e8b\u5b9c\u54a8\u8be2\u7b54\u590d",
            "\u5408\u540c\u5f52\u6863\u6574\u7406\u79fb\u4ea4\u8d44\u6599\u5ba4",
        ],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="replace_field",
                        field="today_work",
                        items=[
                            "\u5b8c\u6210\u65e5\u5e38\u7528\u5370\u6d41\u7a0b\u5ba1\u6279",
                            "\u5b8c\u6210\u73b0\u573a\u7528\u5370\u5ba1\u6838",
                            "\u5b8c\u6210\u7535\u5b50\u7ae0\u7528\u5370",
                            "\u5b8c\u6210\u672a\u5f52\u6863\u5408\u540c\u50ac\u6536",
                        ],
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u6628\u65e5\u660e\u65e5\u8ba1\u5212\u9664\u4e86\u7b2c\u516d\u6761\uff0c\u5176\u4ed6\u90fd\u5df2\u5b8c\u6210",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert store["report"].today_work == [
        "\u5b8c\u6210\u65e5\u5e38\u7528\u5370\u6d41\u7a0b\u5ba1\u6279",
        "\u5b8c\u6210\u73b0\u573a\u7528\u5370\u5ba1\u6838",
        "\u5b8c\u6210\u7535\u5b50\u7ae0\u7528\u5370",
        "\u5b8c\u6210\u672a\u5f52\u6863\u5408\u540c\u50ac\u6536",
        "\u5b8c\u6210\u7528\u5370\u4e8b\u5b9c\u54a8\u8be2\u7b54\u590d",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_input", "today_items", "tomorrow_items"),
    [
        (
            "\u7167\u6628\u5929\u7684\u8ba1\u5212\uff0c\u524d\u4e94\u9879\u90fd\u5b8c\u6210\u4e86\uff0c\u7b2c\u516d\u9879\u6ca1\u505a\u3002",
            ["\u524d\u4e94\u9879\u5df2\u5b8c\u6210", "\u7b2c\u516d\u9879\u6ca1\u505a"],
            [],
        ),
        (
            "\u6628\u5929\u5b89\u6392\u9664\u6700\u540e\u4e00\u9879\u5916\u90fd\u5b8c\u6210\u4e86\uff0c\u6ca1\u6709\u95ee\u9898\u3002",
            ["\u5b8c\u6210\u6628\u5929\u5b89\u6392\u9664\u6700\u540e\u4e00\u9879\u5916\u7684\u4e8b\u9879"],
            [],
        ),
        (
            "\u6628\u65e5\u8ba1\u5212\u4e00\u5230\u4e94\u9879\u5df2\u5b8c\u6210\uff0c\u7b2c\u516d\u9879\u660e\u5929\u7ee7\u7eed\u3002",
            ["\u6628\u65e5\u8ba1\u5212\u4e00\u5230\u4e94\u9879\u5df2\u5b8c\u6210"],
            ["\u7b2c\u516d\u9879\u660e\u5929\u7ee7\u7eed"],
        ),
        (
            "\u6628\u5929\u5b89\u6392\u9664\u6700\u540e\u4e00\u4ef6\u4ee5\u5916\u90fd\u641e\u5b9a\u4e86\uff0c\u6ca1\u6709\u95ee\u9898\u3002",
            ["\u5b8c\u6210\u6628\u5929\u5b89\u6392\u9664\u6700\u540e\u4e00\u4ef6\u4ee5\u5916\u7684\u4e8b\u9879"],
            [],
        ),
    ],
)
async def test_previous_plan_reference_variant_phrasings_expand_safely(monkeypatch, raw_input, today_items, tomorrow_items):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=[
            "\u8ddf\u8fdbA\u5408\u540c",
            "\u5ba1\u6838B\u5408\u540c",
            "\u6574\u7406C\u6750\u6599",
            "\u53d1\u9001D\u51fd\u4ef6",
            "\u5f52\u6863E\u6750\u6599",
            "\u529e\u7406F\u624b\u7eed",
        ],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    actions = [
        AgentAction(type="append_items", field="today_work", items=today_items),
        AgentAction(type="replace_field", field="problems", items=["\u6682\u65e0\u660e\u663e\u95ee\u9898"]),
    ]
    if tomorrow_items:
        actions.append(AgentAction(type="replace_field", field="tomorrow_plan", items=tomorrow_items))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [ActionPlan(intent="fill_report", confidence="high", should_write=True, actions=actions)]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test")

    assert result.report_saved is True
    assert store["report"].today_work == [
        "\u5b8c\u6210\u8ddf\u8fdbA\u5408\u540c",
        "\u5b8c\u6210\u5ba1\u6838B\u5408\u540c",
        "\u5b8c\u6210\u6574\u7406C\u6750\u6599",
        "\u5b8c\u6210\u53d1\u9001D\u51fd\u4ef6",
        "\u5b8c\u6210\u5f52\u6863E\u6750\u6599",
    ]
    assert store["report"].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    expected_tomorrow = ["\u529e\u7406F\u624b\u7eed"] if tomorrow_items else []
    assert store["report"].tomorrow_plan == expected_tomorrow
    joined = "\n".join(store["report"].today_work + store["report"].tomorrow_plan)
    assert "\u6628\u65e5\u8ba1\u5212" not in joined
    assert "\u6628\u5929\u5b89\u6392" not in joined
    assert "\u7b2c\u516d\u9879" not in joined


@pytest.mark.asyncio
async def test_previous_plan_ambiguity_asks_once_without_writing(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=["跟进A合同", "审核B合同", "修改C合同条款"],
        status=STATUS_COMPLETED,
    )
    existing = _report()
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="medium",
                should_write=False,
                actions=[AgentAction(type="ask_clarification")],
                reply_to_user="我找到多个合同相关事项，你说的是哪一项？\n1. 跟进A合同\n2. 审核B合同\n3. 修改C合同条款",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="那个合同完成了", source="test")

    assert result.report_saved is False
    assert store["report"].today_work == []
    assert "多个合同相关事项" in result.message


@pytest.mark.asyncio
async def test_previous_plan_rollover_without_reference_does_not_write(monkeypatch):
    existing = _report()
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="complete_all_previous_plan_items")],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="昨天待办都完成了", source="test")

    assert result.report_saved is False
    assert store["report"].today_work == []
    assert "没有找到昨天的待办内容" in result.message


@pytest.mark.asyncio
async def test_previous_plan_rollover_deduplicates_existing_completion(monkeypatch):
    previous = _report(
        report_date=date(2026, 6, 15),
        tomorrow_plan=["跟进合同B", "整理案件C材料"],
        status=STATUS_COMPLETED,
    )
    existing = _report(today_work=["完成跟进合同B", "完成整理案件C材料"])
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="complete_all_previous_plan_items")],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="昨天待办都完成了", source="test")

    assert result.report_saved is False
    assert store["report"].today_work == ["完成跟进合同B", "完成整理案件C材料"]
    assert "没有重复添加" in result.message
    assert "当前日报草稿" in result.message


@pytest.mark.asyncio
async def test_add_report_content_replies_with_full_preview(monkeypatch):
    existing = _report()
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="append_items", field="today_work", items=["审核18份合同"])],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="今天审核18份合同", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["审核18份合同"]
    assert "当前日报草稿" in result.message
    assert "今日工作：" in result.message
    assert "问题/风险：" in result.message
    assert "明日计划：" in result.message


@pytest.mark.asyncio
async def test_update_report_content_replies_with_full_preview(monkeypatch):
    existing = _report(today_work=["审核18份合同"], problems=["暂无明显问题"], tomorrow_plan=["无明日计划"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="replace_text",
                        field="today_work",
                        target_item_index=1,
                        old_value="18份",
                        new_value="20份",
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="把18份改成20份", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["审核20份合同"]
    assert "当前日报草稿" in result.message
    assert "审核20份合同" in result.message


def _current_edit_pending(today_work, problems, tomorrow_plan, *, status=STATUS_COLLECTING):
    return {
        "type": "current_report_edit_flow",
        "operation": "modify_report",
        "target_field": "none",
        "context": {
            "target_date": "2026-06-16",
            "stage": "awaiting_edit_instruction",
            "current_report": {
                "today_work": list(today_work),
                "problems": list(problems),
                "tomorrow_plan": list(tomorrow_plan),
                "status": status,
            },
        },
    }


@pytest.mark.asyncio
async def test_current_report_edit_flow_executes_multiple_actions_in_one_sentence(monkeypatch):
    today_work = ["梳理合同资料", "统计用印数据", "整理风控台账"]
    problems = ["办公系统加载慢", "部分合同信息不全", "文件摆放混乱有遗失风险"]
    tomorrow_plan = ["补齐合同缺失信息", "对接项目修改协议", "汇总机器人测试问题记录"]
    existing = _report(
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        section_status={"_pending_interaction": _current_edit_pending(today_work, problems, tomorrow_plan)},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="今日工作第2条换成审核了六十份合同，风险第3条删掉",
        source="test",
    )

    assert result.report_saved is True
    assert service.report_agent.calls == []
    assert store["report"].today_work == ["梳理合同资料", "审核了六十份合同", "整理风控台账"]
    assert store["report"].problems == ["办公系统加载慢", "部分合同信息不全"]
    assert "审核了六十份合同" in result.message
    assert "已删除问题/风险第3条" in result.message


@pytest.mark.asyncio
async def test_draft_single_text_delete_executes_without_confirmation(monkeypatch):
    today_work = ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"]
    problems = ["发现部分工人未签劳动合同"]
    tomorrow_plan = ["去苏州开庭"]
    existing = _report(
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        section_status={"_pending_interaction": _current_edit_pending(today_work, problems, tomorrow_plan)},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="函件那条删掉", source="test")

    assert result.report_saved is True
    assert "确认删除" not in result.message
    assert "如需恢复" in result.message
    assert store["report"].today_work == ["出差去南京开庭", "审核8份合同", "处理工人讨薪"]


@pytest.mark.asyncio
async def test_draft_single_delete_can_be_undone_to_original_position(monkeypatch):
    today_work = ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"]
    problems = ["发现部分工人未签劳动合同"]
    tomorrow_plan = ["去苏州开庭"]
    existing = _report(
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        section_status={"_pending_interaction": _current_edit_pending(today_work, problems, tomorrow_plan)},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    await service.submit_text(FakeSession(), user=_user(), raw_input="函件那条删掉", source="test")
    result = await service.submit_text(FakeSession(), user=_user(), raw_input="算了不删了，加回去吧", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == today_work
    assert "撰写10份函件" in result.message


@pytest.mark.asyncio
async def test_completed_report_edit_executes_without_withdraw_confirmation(monkeypatch):
    user = _user()
    today_work = ["出差去南京开庭", "审核8份合同", "撰写10份函件", "处理工人讨薪"]
    problems = ["发现部分工人未签劳动合同"]
    tomorrow_plan = ["去苏州开庭"]
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        section_status={"_pending_interaction": _current_edit_pending(today_work, problems, tomorrow_plan, status=STATUS_COMPLETED)},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="delete_item", field="today_work", item_indices=[3])],
                reason="simulated semantic delete of the third item",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=user, raw_input="第三条删掉吧", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert "是否撤回并修改" not in result.message
    assert "确认删除" not in result.message
    assert store["report"].status == STATUS_COMPLETED
    assert store["report"].today_work == ["出差去南京开庭", "审核8份合同", "处理工人讨薪"]
    pending = store["report"].section_status.get("_pending_interaction")
    assert pending is None or pending["type"] != "awaiting_action_confirmation"


@pytest.mark.asyncio
async def test_pending_delete_is_replaced_by_new_unsubmit_intent(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["出差去南京开庭", "审核8份合同", "撰写10份函件"],
        problems=[],
        tomorrow_plan=["去苏州开庭"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_report_item",
                "target_field": "today_work",
                "context": {"item_indices": [3], "action": {"type": "delete_item", "field": "today_work", "item_indices": [3]}},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="可以给我撤回提交的日报么", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert "确认撤回" not in result.message
    assert store["report"].status == STATUS_COLLECTING
    assert "_pending_interaction" not in store["report"].section_status
    assert "等待上一项操作" not in result.message


@pytest.mark.asyncio
async def test_withdraw_and_modify_phrase_unsubmits_before_current_edit_flow(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["审核3份合同", "整理用印材料"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天继续推进"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="我要撤回日报改一下", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert result.reply_kind == "agent_unsubmit_report"
    assert store["report"].status == STATUS_COLLECTING
    assert "已撤回日报" in result.message
    assert "先把今天的日报调出来" not in result.message


@pytest.mark.asyncio
async def test_noisy_confirmation_executes_pending_batch_once(monkeypatch):
    existing = _report(
        today_work=["第一条", "第二条"],
        section_status={
            "_pending_interaction": {
                "type": "pending_batch_action",
                "operation": "batch_action",
                "target_field": "today_work",
                "context": {
                    "actions": [{"type": "delete_item", "field": "today_work", "item_indices": [1]}],
                },
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="读哦\n对", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["第二条"]
    assert "没太明白" not in result.message
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_pending_split_or_append_confirmation_executes_suggestion(monkeypatch):
    existing = _report(
        today_work=["日常用印资料审核"],
        problems=[],
        tomorrow_plan=["明日计划来函进展继续跟进闭环，帮助律师归还借阅资料"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_clarification",
                "operation": "split_or_append",
                "target_field": "tomorrow_plan",
                "context": {
                    "current_item_text": "明日计划来函进展继续跟进闭环，帮助律师归还借阅资料",
                    "current_item_index": 1,
                    "split_suggestion": ["来函进展继续跟进闭环", "帮助律师归还借阅资料"],
                },
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="对的", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["来函进展继续跟进闭环", "帮助律师归还借阅资料"]
    assert "_pending_interaction" not in store["report"].section_status
    assert "specific pending detail" not in result.message
    assert not service.report_agent.calls


@pytest.mark.asyncio
async def test_local_delete_content_phrase_does_not_trigger_global_clear(monkeypatch):
    existing = _report(
        today_work=["进行上海机载（机器的机载重的载）项目评审"],
        problems=["在常州豫园起诉资料起草中确认管辖"],
        tomorrow_plan=["上石项目木石面扣款汇报"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="replace_text",
                        field="today_work",
                        old_value="进行上海机载（机器的机载重的载）项目评审",
                        new_value="进行上海机载项目评审",
                        target_item_index=1,
                    )
                ],
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="上海记载是对的，括号的内容删掉。第二个就是上实实是实在的实",
        source="test",
    )

    assert result.report_saved is True
    assert store["report"].today_work == ["进行上海机载项目评审"]
    assert store["report"].problems == ["在常州豫园起诉资料起草中确认管辖"]
    assert store["report"].tomorrow_plan == ["上石项目木石面扣款汇报"]
    assert service.report_agent.calls
    assert "确认清空" not in result.message


@pytest.mark.asyncio
async def test_completed_direct_tomorrow_plan_withdraws_and_keeps_original_edit(monkeypatch):
    user = _user()
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["整理合同资料"],
        problems=["无"],
        tomorrow_plan=[],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=user, raw_input="明天去北京出差", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert store["report"].status == STATUS_COMPLETED
    assert "是否撤回并修改" not in result.message
    assert store["report"].tomorrow_plan == ["明天去北京出差"]
    assert "_pending_interaction" not in store["report"].section_status
    assert "确认撤回" not in result.message
    assert "去北京出差" in result.message


@pytest.mark.asyncio
async def test_relative_duplicate_delete_after_range_merge_does_not_delete_merged_item(monkeypatch):
    today_work = [
        "优化日报发送逻辑",
        "线上签署1份增补合同",
        "沟通服务器方案",
        "下载柬埔寨民法典英文版与高棉文版",
        "线上签署云筑网福建项目5月产值申报",
        "填报日期更清楚了",
        "9:00规则收紧了",
        "编辑能力稳定了一轮",
        "自动提交逻辑更保守",
        "长文本枚举归属修了",
        "LLM使用更稳了",
        "影子记忆系统",
    ]
    existing = _report(today_work=today_work, problems=[], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="merge_items", field="today_work", item_indices=[6, 7, 8, 9, 10, 11, 12])],
                reason="merge tail items",
            ),
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="delete_item", field="today_work", item_indices=[6])],
                reason="simulated unsafe relative duplicate delete",
            ),
        ]
    )

    merged = await service.submit_text(FakeSession(), user=_user(), raw_input="6到12条是同一条", source="test")
    result = await service.submit_text(FakeSession(), user=_user(), raw_input="删掉后面重复的", source="test")

    assert merged.report_saved is True
    assert len(store["report"].today_work) == 6
    assert "填报日期" in store["report"].today_work[5]
    assert result.report_saved is False
    assert len(store["report"].today_work) == 6
    assert "填报日期" in store["report"].today_work[5]
    assert "定位清楚" in result.message


@pytest.mark.asyncio
async def test_completed_report_clear_then_display_is_draft_status(monkeypatch):
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["整理合同资料"],
        problems=["无"],
        tomorrow_plan=["去北京出差"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="clear_all")],
            ),
            ActionPlan(intent="query_current", confidence="high", should_write=False, actions=[]),
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="清空今天日报", source="test", report_date=date(2026, 6, 16))
    display = await service.submit_text(FakeSession(), user=_user(), raw_input="展示我现在的日报", source="test", report_date=date(2026, 6, 16))

    assert result.report_saved is True
    assert "是否撤回并修改" not in result.message
    assert store["report"].status == STATUS_COLLECTING
    assert store["report"].today_work == []
    assert store["report"].problems == []
    assert store["report"].tomorrow_plan == []
    assert "【状态】** 填写中" in display.message
    assert "【状态】** 已提交" not in display.message


@pytest.mark.asyncio
async def test_after_withdrawn_status_draft_adds_without_second_withdraw_prompt(monkeypatch):
    user = _user()
    existing = _report(
        status=STATUS_COMPLETED,
        today_work=["整理合同资料"],
        problems=["无"],
        tomorrow_plan=[],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    first = await service.submit_text(FakeSession(), user=user, raw_input="明天去北京出差", source="test", report_date=date(2026, 6, 16))
    again = await service.submit_text(FakeSession(), user=user, raw_input="明天去上海出差", source="test", report_date=date(2026, 6, 16))

    assert first.report_saved is True
    assert again.report_saved is True
    assert store["report"].status == STATUS_COMPLETED
    assert store["report"].tomorrow_plan == ["明天去北京出差", "明天去上海出差"]
    assert "是否撤回并修改" not in again.message
    assert "当前日报草稿" in again.message


@pytest.mark.asyncio
async def test_append_signal_adds_tomorrow_plan_without_overwriting_or_inheriting(monkeypatch):
    tomorrow_plan = ["去北京出差评审合同"]
    existing = _report(
        today_work=["整理合同资料"],
        problems=["无"],
        tomorrow_plan=tomorrow_plan,
        section_status={"_pending_interaction": _current_edit_pending(["整理合同资料"], ["无"], tomorrow_plan)},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="明天还会去趟南京", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["去北京出差评审合同", "去南京"]
    assert "去南京出差评审合同" not in store["report"].tomorrow_plan
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_also_go_restores_overwritten_old_tomorrow_plan_and_keeps_new_item(monkeypatch):
    user = _user()
    report_id = uuid4()
    existing = _report(
        id=report_id,
        tomorrow_plan=["去南京出差评审合同"],
        section_status={
            "_last_modified_item": {
                "report_id": str(report_id),
                "report_date": "2026-06-16",
                "user_id": str(user.id),
                "section": "tomorrow_plan",
                "item_index": 1,
                "old_content": "去北京出差评审合同",
                "new_content": "去南京出差评审合同",
                "source_user_message": "把北京改成南京",
            },
            "_correction_target": {
                "report_id": str(report_id),
                "report_date": "2026-06-16",
                "user_id": str(user.id),
                "section": "tomorrow_plan",
                "item_index": 1,
                "old_content": "去北京出差评审合同",
                "new_content": "去南京出差评审合同",
                "source_user_message": "把北京改成南京",
            },
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=user, raw_input="北京也去的啊", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["去北京出差评审合同", "去南京"]
    assert "抱歉" in result.message
    assert "去北京出差评审合同" in result.message
    assert "去南京" in result.message
    assert "请确认" not in result.message


@pytest.mark.asyncio
async def test_not_replace_but_also_add_restores_old_tomorrow_plan(monkeypatch):
    user = _user()
    report_id = uuid4()
    existing = _report(
        id=report_id,
        tomorrow_plan=["去南京出差评审合同"],
        section_status={
            "_last_modified_item": {
                "report_id": str(report_id),
                "report_date": "2026-06-16",
                "user_id": str(user.id),
                "section": "tomorrow_plan",
                "item_index": 1,
                "old_content": "去北京出差评审合同",
                "new_content": "去南京出差评审合同",
                "source_user_message": "把北京改成南京",
            },
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="不是改成南京，是还要去南京",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert store["report"].tomorrow_plan == ["去北京出差评审合同", "去南京"]


@pytest.mark.asyncio
async def test_negative_replacement_updates_existing_tomorrow_plan_without_appending(monkeypatch):
    existing = _report(
        today_work=["审了三个合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天去南京开庭"],
        status=STATUS_PENDING_CONFIRMATION,
        section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="明天不是去南京，是去上海开庭。",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["明天去上海开庭"]
    assert "南京" not in store["report"].tomorrow_plan[0]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_correction_removes_unsupported_detail_from_last_modified_item(monkeypatch):
    user = _user()
    report_id = uuid4()
    existing = _report(
        id=report_id,
        tomorrow_plan=["去北京出差，进行合同交底"],
        section_status={
            "_last_modified_item": {
                "report_id": str(report_id),
                "report_date": "2026-06-16",
                "user_id": str(user.id),
                "section": "tomorrow_plan",
                "item_index": 1,
                "old_content": "",
                "new_content": "去北京出差，进行合同交底",
                "source_user_message": "明日计划是去北京",
            },
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=user, raw_input="我没说是去合同交底啊", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["去北京出差"]
    assert "合同交底" not in store["report"].tomorrow_plan[0]


@pytest.mark.asyncio
async def test_fragment_correction_updates_last_modified_tomorrow_plan(monkeypatch):
    user = _user()
    report_id = uuid4()
    existing = _report(
        id=report_id,
        tomorrow_plan=["去北京出差"],
        section_status={
            "_last_modified_item": {
                "report_id": str(report_id),
                "report_date": "2026-06-16",
                "user_id": str(user.id),
                "section": "tomorrow_plan",
                "item_index": 1,
                "old_content": "",
                "new_content": "去北京出差",
                "source_user_message": "明天去北京出差",
            },
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    await service.submit_text(FakeSession(), user=user, raw_input="去开庭的", source="test")

    assert store["report"].tomorrow_plan == ["去北京开庭"]


@pytest.mark.asyncio
async def test_delete_recently_appended_daily_item_without_clarification(monkeypatch):
    existing = _report(
        today_work=["审核3份合同", "整理材料", "沟通服务器方案"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天继续推进"],
        status=STATUS_PENDING_CONFIRMATION,
        section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    appended = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="另外补一个，整理会议纪要。",
        source="test",
        report_date=date(2026, 6, 16),
    )
    deleted = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="刚补的那条删掉。",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert appended.report_saved is True
    assert deleted.report_saved is True
    assert store["report"].today_work == ["审核3份合同", "整理材料", "沟通服务器方案"]
    assert "哪一部分" not in deleted.message
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_fact_source_guard_blocks_agent_from_importing_unmentioned_tomorrow_detail(monkeypatch):
    plan = ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="append_items", field="tomorrow_plan", items=["去北京出差评审合同"])],
    )
    existing = _report(today_work=["整理合同资料"], problems=["无"], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([plan])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="计划去北京", source="test")

    assert result.report_saved is False
    assert store["report"].tomorrow_plan == []
    assert "没有明确说明" in result.message


@pytest.mark.asyncio
async def test_historical_report_delete_is_blocked(monkeypatch):
    existing = _report(today_work=["整理合同资料"], problems=["无"], tomorrow_plan=["去北京"])
    previous = _report(report_date=date(2026, 6, 15), today_work=["昨天工作"], problems=[], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="把昨天日报删了", source="test")

    assert result.report_saved is False
    assert "历史日报不能删除" in result.message
    assert store["previous"].today_work == ["昨天工作"]


@pytest.mark.asyncio
async def test_employee_cannot_view_or_operate_other_user_report(monkeypatch):
    existing = _report(today_work=["整理合同资料"], problems=["无"], tomorrow_plan=["去北京"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="把张三昨天日报发我看看", source="test")

    assert result.report_saved is False
    assert "没有查看或操作该人员日报的权限" in result.message
    assert store["report"].today_work == ["整理合同资料"]


@pytest.mark.asyncio
async def test_update_records_scoped_last_modified_item_for_correction(monkeypatch):
    user = _user()
    existing = _report(
        today_work=["审核8份合同"],
        problems=["无"],
        tomorrow_plan=["去北京"],
        section_status={"_pending_interaction": _current_edit_pending(["审核8份合同"], ["无"], ["去北京"])},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    await service.submit_text(FakeSession(), user=user, raw_input="今日工作第1条改成审核10份合同", source="test")
    target = store["report"].section_status["_last_modified_item"]

    assert target["user_id"] == str(user.id)
    assert target["report_date"] == "2026-06-16"
    assert target["report_id"] == str(existing.id)
    assert target["section"] == "today_work"
    assert target["item_index"] == 1
    assert target["old_content"] == "审核8份合同"
    assert target["new_content"] == "审核10份合同"
    assert target["source_user_message"] == "今日工作第1条改成审核10份合同"


@pytest.mark.asyncio
async def test_after_daily_lock_blocks_new_report_content(monkeypatch):
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 9, 1))
    existing = _report(today_work=["整理合同资料"], problems=["无"], tomorrow_plan=["去北京"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="明天去南京出差", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "daily_report_locked"
    assert "2026-06-17 09:00" in result.message
    assert "不能修改、删除、撤回或提交" in result.message
    assert store["report"].tomorrow_plan == ["去北京"]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_after_daily_lock_blocks_pending_confirmation(monkeypatch):
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 9, 1))
    existing = _report(
        today_work=["第一条", "第二条"],
        section_status={
            "_pending_interaction": {
                "type": "pending_batch_action",
                "operation": "batch_action",
                "target_field": "today_work",
                "context": {
                    "actions": [{"type": "delete_item", "field": "today_work", "item_indices": [1]}],
                },
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="确认", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "daily_report_locked"
    assert "09:00" in result.message
    assert store["report"].today_work == ["第一条", "第二条"]
    assert store["report"].section_status["_pending_interaction"]["operation"] == "batch_action"
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_after_cutoff_blocks_short_yesterday_content(monkeypatch):
    store = _install_store(monkeypatch, None)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 20, 11))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u6628\u5929\u5ba1\u6838\u4e86\u5408\u540c\u3002", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "previous_report_cutoff"
    assert "09:00" in result.message
    assert store["report"] is None
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_current_edit_flow_bare_confirmation_submits_complete_pending_report(monkeypatch):
    existing = _report(
        status=STATUS_PENDING_CONFIRMATION,
        today_work=["\u5ba1\u68383\u4efd\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["\u660e\u5929\u7ee7\u7eed\u63a8\u8fdb"],
        section_status={
            "today_work": True,
            "problems": True,
            "tomorrow_plan": True,
            "_pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "today_work",
                "context": {"stage": "awaiting_field_edit_content"},
            },
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u786e\u8ba4",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert result.report_saved is True
    assert result.reply_kind == "agent_confirm_submit"
    assert result.status == STATUS_COMPLETED
    assert store["report"].status == STATUS_COMPLETED
    assert store["report"].confirmed_by_user is True
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_after_daily_lock_allows_current_report_display(monkeypatch):
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 9, 1))
    existing = _report(today_work=["整理合同资料"], problems=["无"], tomorrow_plan=["去北京"])
    _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="展示我现在的日报", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "history_query"
    assert "整理合同资料" in result.message
    assert "去北京" in result.message
    assert "【状态】** 填写中" in result.message
    assert "不能修改" not in result.message
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_before_daily_lock_still_allows_report_content(monkeypatch):
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 8, 59))
    existing = _report(today_work=["整理合同资料"], problems=["无"], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="明天去南京出差", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["去南京出差"]
    assert "09:00" not in result.message


@pytest.mark.asyncio
async def test_same_report_day_after_nine_still_allows_report_content(monkeypatch):
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 15, 14))
    existing = _report(today_work=["整理合同资料"], problems=["无"], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="明天去南京出差", source="test")

    assert result.report_saved is True
    assert store["report"].tomorrow_plan == ["去南京出差"]
    assert result.reply_kind != "daily_report_locked"
@pytest.mark.asyncio
async def test_agent_integrated_plan_item_is_not_split_by_executor(monkeypatch):
    store = _install_store(monkeypatch, _report())
    service = DailyReportService(_settings(), FakeExtractor())
    integrated_item = "制定日报系统下一步计划：与原告底表结合，构建原告案件进展系统，实现日报中提及案件时自动关联并补充进展，以及在固定时间和节点询问进展"
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="append_items",
                        field="today_work",
                        items=[
                            "优化合同评审skill，提升技能稳定性",
                            integrated_item,
                            "处理下游合同线下切换及答疑",
                            "沟通成都住建局电子章事宜",
                        ],
                    ),
                    AgentAction(type="append_items", field="problems", items=["合同评审skill的token消耗和时间消耗仍偏高"]),
                ],
                reason="agent kept one integrated plan item",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="今天按你理解整理一下", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == [
        "优化合同评审skill，提升技能稳定性",
        integrated_item,
        "处理下游合同线下切换及答疑",
        "沟通成都住建局电子章事宜",
    ]
    assert len(service.report_agent.calls) == 1


@pytest.mark.asyncio
async def test_agent_merge_items_action_merges_existing_items(monkeypatch):
    existing = _report(
        today_work=[
            "优化合同评审skill，提升技能稳定性",
            "制定日报系统下一步计划：与原告底表结合",
            "构建原告案件进展系统",
            "实现日报中提及案件时自动关联并补充进展",
            "处理下游合同线下切换及答疑",
        ],
        problems=["合同评审skill的token消耗和时间消耗仍偏高"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="merge_items", field="today_work", item_indices=[2, 3, 4])],
                reason="agent understood user correction as item merge",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="这三个是同一条", source="test")

    assert result.report_saved is True
    assert "已合并今日工作第2、3、4条" in result.message
    assert store["report"].today_work == [
        "优化合同评审skill，提升技能稳定性",
        "制定日报系统下一步计划：与原告底表结合，构建原告案件进展系统，实现日报中提及案件时自动关联并补充进展",
        "处理下游合同线下切换及答疑",
    ]
    assert len(service.report_agent.calls) == 1


@pytest.mark.asyncio
async def test_current_edit_flow_natural_merge_delegates_to_report_agent(monkeypatch):
    today_work = [
        "优化合同评审skill，提升技能稳定性",
        "制定日报系统下一步计划：与原告底表结合",
        "构建原告案件进展系统",
        "处理下游合同线下切换及答疑",
    ]
    existing = _report(
        today_work=today_work,
        problems=["合同评审skill的token消耗和时间消耗仍偏高"],
        section_status={"_pending_interaction": _current_edit_pending(today_work, ["合同评审skill的token消耗和时间消耗仍偏高"], [])},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="merge_items", field="today_work", item_indices=[2, 3])],
                reason="agent understood merge inside current edit flow",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="合并今日工作的2、3", source="test")

    assert result.report_saved is True
    assert "已合并今日工作第2、3条" in result.message
    assert store["report"].today_work == [
        "优化合同评审skill，提升技能稳定性",
        "制定日报系统下一步计划：与原告底表结合，构建原告案件进展系统",
        "处理下游合同线下切换及答疑",
    ]
    assert len(service.report_agent.calls) == 1


@pytest.mark.asyncio
async def test_direct_merge_numbered_today_work_items(monkeypatch):
    existing = _report(
        today_work=[
            "优化合同评审skill，提升技能稳定性",
            "制定日报系统下一步计划：与原告底表结合",
            "构建原告案件进展系统",
            "实现日报中提及案件时自动关联并补充进展",
            "以及在固定时间和节点询问进展",
            "处理下游合同线下切换及答疑",
            "沟通成都住建局电子章事宜",
        ],
        problems=["合同评审skill的token消耗和时间消耗仍偏高"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="merge_items", field="today_work", item_indices=[2, 3, 4, 5])],
                reason="agent understood numbered merge",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="合并今日工作的  2，3，4，5", source="test")

    assert result.report_saved is True
    assert "已合并今日工作第2、3、4、5条" in result.message
    assert store["report"].today_work == [
        "优化合同评审skill，提升技能稳定性",
        "制定日报系统下一步计划：与原告底表结合，构建原告案件进展系统，实现日报中提及案件时自动关联并补充进展，以及在固定时间和节点询问进展",
        "处理下游合同线下切换及答疑",
        "沟通成都住建局电子章事宜",
    ]
    assert len(service.report_agent.calls) == 1


@pytest.mark.asyncio
async def test_direct_same_point_range_merge_infers_today_work(monkeypatch):
    existing = _report(
        today_work=[
            "优化合同评审skill，提升技能稳定性",
            "制定日报系统下一步计划：与原告底表结合",
            "构建原告案件进展系统",
            "实现日报中提及案件时自动关联并补充进展",
            "以及在月底",
            "开庭前后",
            "判决前后等固定时间节点询问进展",
        ],
        problems=["合同评审skill的token消耗和时间消耗仍偏高"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="merge_items", field="today_work", item_indices=[2, 3, 4, 5, 6, 7])],
                reason="agent understood range merge",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="第二条到第七条是同一点啊", source="test")

    assert result.report_saved is True
    assert "已合并今日工作第2、3、4、5、6、7条" in result.message
    assert store["report"].today_work == [
        "优化合同评审skill，提升技能稳定性",
        "制定日报系统下一步计划：与原告底表结合，构建原告案件进展系统，实现日报中提及案件时自动关联并补充进展，以及在月底，开庭前后，判决前后等固定时间节点询问进展",
    ]
    assert store["report"].problems == ["合同评审skill的token消耗和时间消耗仍偏高"]
    assert len(service.report_agent.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input, indices",
    [
        ("2-7\u662f\u540c\u4e00\u70b9", [2, 3, 4, 5, 6, 7]),
        ("\u7b2c\u4e8c\u6761\u5230\u7b2c\u4e03\u6761\u662f\u540c\u4e00\u70b9\u554a", [2, 3, 4, 5, 6, 7]),
        ("3\u30014\u30015\u8fd9\u4e09\u4e2a\u662f\u540c\u4e00\u6761", [3, 4, 5]),
        ("\u548c\u5e76\u4eca\u65e5\u5de5\u4f5c\u7684 2\uff0c3\uff0c4\uff0c5", [2, 3, 4, 5]),
        ("\u5408\u5e76\u4eca\u65e5\u5de5\u4f5c\u7684 2\uff0c3\uff0c4\uff0c5", [2, 3, 4, 5]),
        ("\u5e76\u4eca\u65e5\u5de5\u4f5c\u7684 2\u30013\u30014\u30015", [2, 3, 4, 5]),
        ("\u8fd9\u51e0\u6761\u662f\u4e00\u56de\u4e8b", [2, 3, 4]),
        ("\u4e0d\u8981\u62c6\u8fd9\u4e48\u788e", [2, 3, 4]),
        ("\u8fd9\u51e0\u6761\u5408\u5e76\u540c\u7c7b\u9879", [2, 3, 4]),
    ],
)
async def test_merge_like_delete_item_output_is_coerced_to_merge(monkeypatch, raw_input, indices):
    existing = _report(
        today_work=[
            "A",
            "B",
            "C",
            "D",
            "E",
            "F",
            "G",
        ],
        problems=["P"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="delete_item", field="today_work", item_indices=indices)],
                reason="simulated bad LLM action for merge-like text",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test")

    assert result.report_saved is True
    assert "\u6ca1\u6709\u627e\u5230\u8981\u5220\u9664\u7684\u6761\u76ee" not in result.message
    assert "\u5df2\u5408\u5e76\u4eca\u65e5\u5de5\u4f5c" in result.message
    assert len(store["report"].today_work) == 7 - len(indices) + 1
    assert len(service.report_agent.calls) == 1


@pytest.mark.asyncio
async def test_merge_like_delete_item_without_safe_target_asks_clarification(monkeypatch):
    existing = _report(today_work=["A", "B", "C"], problems=["P"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="delete_item", field="none", item_indices=[2, 3])],
                reason="simulated bad LLM action without field",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u8fd9\u51e0\u6761\u662f\u4e00\u56de\u4e8b", source="test")

    assert result.report_saved is False
    assert "\u6ca1\u6709\u627e\u5230\u8981\u5220\u9664\u7684\u6761\u76ee" not in result.message
    assert "\u6211\u7406\u89e3\u4f60\u662f\u60f3\u5408\u5e76\u8fd9\u4e9b\u6761\u76ee" in result.message
    assert store["report"].today_work == ["A", "B", "C"]


def test_report_agent_prompt_action_list_matches_schema_for_unsubmit_report():
    prompt = Path("app/agent/prompts/report_agent.md").read_text(encoding="utf-8")
    schema_actions = set(AgentAction.model_fields["type"].annotation.__args__)
    assert "unsubmit_report" in schema_actions
    assert "unsubmit_report" in prompt

@pytest.mark.asyncio
async def test_no_write_merge_action_with_edit_intent_executes_when_safe(monkeypatch):
    existing = _report(
        today_work=["A", "B", "C", "D", "E", "F", "G"],
        problems=["P"],
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                actions=[AgentAction(type="merge_items", field="today_work", item_indices=[2, 3, 4, 5, 6, 7])],
                reply_to_user="\u5df2\u7ecf\u5e2e\u4f60\u5408\u5e76\u4e86\u8fd9\u4e9b\u6761\u76ee\u3002",
                reason="simulated no-write merge contradiction",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u4e0d\u8981\u62c6\u8fd9\u4e48\u788e", source="test")

    assert result.report_saved is True
    assert "\u5df2\u5408\u5e76\u4eca\u65e5\u5de5\u4f5c\u7b2c2\u30013\u30014\u30015\u30016\u30017\u6761" in result.message
    assert store["report"].today_work == ["A", "B\uff0cC\uff0cD\uff0cE\uff0cF\uff0cG"]


@pytest.mark.asyncio
async def test_no_write_unsafe_write_action_with_edit_intent_asks_clarification(monkeypatch):
    existing = _report(today_work=["A", "B", "C"], problems=["P"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                actions=[AgentAction(type="merge_items", field="none", item_indices=[2, 3])],
                reply_to_user="\u5df2\u7ecf\u5e2e\u4f60\u5408\u5e76\u4e86\u8fd9\u4e9b\u6761\u76ee\u3002",
                reason="simulated unsafe no-write action",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u4e0d\u8981\u62c6\u8fd9\u4e48\u788e", source="test")

    assert result.report_saved is False
    assert "\u5df2\u7ecf\u5e2e\u4f60\u5408\u5e76" not in result.message
    assert "\u6211\u7406\u89e3\u4f60\u662f\u60f3\u5408\u5e76\u8fd9\u4e9b\u6761\u76ee" in result.message
    assert store["report"].today_work == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_no_write_non_edit_no_op_stays_no_write(monkeypatch):
    existing = _report(today_work=["A", "B"], problems=["P"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="emotional_feedback",
                confidence="high",
                should_write=False,
                actions=[AgentAction(type="no_op")],
                reply_to_user="\u6211\u5728\uff0c\u4f60\u53ef\u4ee5\u7ee7\u7eed\u8bf4\u3002",
                reason="chat",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u8c22\u8c22", source="test")

    assert result.report_saved is False
    assert "\u6211\u5728\uff0c\u4f60\u53ef\u4ee5\u7ee7\u7eed\u8bf4\u3002" in result.message
    assert "\u4e0d\u5199\u5165\u65e5\u62a5\u6216\u590d\u76d8" in result.message
    assert store["report"].today_work == ["A", "B"]


@pytest.mark.asyncio
async def test_short_repair_feedback_is_not_written_as_today_work(monkeypatch):
    existing = _report(report_date=date(2026, 6, 16), today_work=["\u5408\u540c\u5ba1\u6838"], problems=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u5565\u73a9\u610f", source="test")

    assert result.report_saved is False
    assert store["report"].today_work == ["\u5408\u540c\u5ba1\u6838"]
    assert "\u5565\u73a9\u610f" not in store["report"].today_work


@pytest.mark.asyncio
async def test_bare_send_me_current_report_is_read_only(monkeypatch):
    existing = _report(report_date=date(2026, 6, 16), today_work=["\u5408\u540c\u5ba1\u6838"], problems=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u53d1\u6211",
        source="test",
        report_date=date(2026, 6, 16),
    )

    assert "\u5408\u540c\u5ba1\u6838" in result.message
    assert store["report"].today_work == ["\u5408\u540c\u5ba1\u6838"]


@pytest.mark.asyncio
async def test_no_write_query_history_is_not_treated_as_write_action(monkeypatch):
    existing = _report(today_work=["A", "B"], problems=["P"])
    previous = _report(report_date=date(2026, 6, 15), today_work=["old"], problems=["old problem"])
    store = _install_store(monkeypatch, existing, previous=previous)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="query_history",
                confidence="high",
                should_write=False,
                actions=[AgentAction(type="query_history", target_date="2026-06-15")],
                reply_to_user="\u6628\u5929\u7684\u65e5\u62a5\u5982\u4e0b\u3002",
                reason="query only",
            )
        ]
    )

    result = await agent_executor.ReportAgentExecutor().execute(
        FakeSession(),
        user=_user(),
        existing=existing,
        report_date=date(2026, 6, 16),
        received_at=datetime(2026, 6, 16, 8, 0),
        raw_input="\u67e5\u770b\u5386\u53f2\u65e5\u62a5",
        source="test",
        plan=service.report_agent.plans[0],
        meta={"model": "fake-agent"},
    )

    assert "old" in result.message
    assert store["report"].today_work == ["A", "B"]


@pytest.mark.asyncio
async def test_no_write_delete_action_with_delete_intent_executes_when_safe(monkeypatch):
    existing = _report(today_work=["A", "B", "C"], problems=["P"])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                actions=[AgentAction(type="delete_item", field="today_work", item_indices=[2])],
                reply_to_user="\u5df2\u5220\u9664\u4eca\u65e5\u5de5\u4f5c\u7b2c2\u6761\u3002",
                reason="simulated no-write delete contradiction",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u5220\u9664\u4eca\u65e5\u5de5\u4f5c\u7684\u7b2c2\u6761", source="test")

    assert result.report_saved is True
    assert "\u5df2\u5220\u9664\u4eca\u65e5\u5de5\u4f5c\u7b2c2\u6761" in result.message
    assert store["report"].today_work == ["A", "C"]


@pytest.mark.asyncio
async def test_short_ordinal_delete_uses_direct_rule_before_llm(monkeypatch):
    existing = _report(
        today_work=["处理部门费用", "更新函件管理办法", "整理归档材料", "沟通服务器方案", "完善日报系统"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天继续推进"],
        status=STATUS_PENDING_CONFIRMATION,
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="第2条不要了", source="test")

    assert result.report_saved is True
    assert store["report"].today_work == ["处理部门费用", "整理归档材料", "沟通服务器方案", "完善日报系统"]
    assert store["report"].problems == ["暂无明显问题"]
    assert not service.report_agent.calls


@pytest.mark.asyncio
async def test_completed_short_ordinal_delete_keeps_submitted_context(monkeypatch):
    existing = _report(
        today_work=["审核3份合同", "整理用印材料", "沟通服务器方案"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天继续推进"],
        status=STATUS_COMPLETED,
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="已提交那份日报删掉第二条", source="test")

    assert result.report_saved is True
    assert "提交" in result.message
    assert "整理用印材料" in result.message
    assert store["report"].status == STATUS_COMPLETED
    assert store["report"].section_status["_pending_interaction"]["operation"] == "delete_report_item"
    assert not service.report_agent.calls


@pytest.mark.asyncio
async def test_short_clear_reply_requests_confirmation_before_clearing(monkeypatch):
    existing = _report(
        today_work=["\u5f00\u4f1a\u8fc7\u6848\u4ef6"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=[],
        section_status={"today_work": True, "problems": True, "tomorrow_plan": False, "problems_acknowledged_empty": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u6e05\u7a7a\u5427", source="test")

    assert result.report_saved is True
    assert result.reply_kind == "agent_pending_action_confirmation"
    assert "\u786e\u8ba4\u6e05\u7a7a\u5f53\u524d\u65e5\u62a5\u5185\u5bb9" in result.message
    pending = store["report"].section_status["_pending_interaction"]
    assert pending["type"] == "awaiting_action_confirmation"
    assert pending["operation"] == "clear_report"
    assert store["report"].today_work == ["\u5f00\u4f1a\u8fc7\u6848\u4ef6"]


@pytest.mark.asyncio
async def test_short_ack_confirms_pending_clear_action(monkeypatch):
    existing = _report(
        today_work=["\u5f00\u4f1a\u8fc7\u6848\u4ef6"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=[],
        section_status={"today_work": True, "problems": True, "tomorrow_plan": False, "problems_acknowledged_empty": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    first = await service.submit_text(FakeSession(), user=_user(), raw_input="\u6e05\u7a7a\u5427", source="test")
    second = await service.submit_text(FakeSession(), user=_user(), raw_input="\u55ef", source="test")

    assert first.reply_kind == "agent_pending_action_confirmation"
    assert second.report_saved is True
    assert second.today_work == []
    assert second.problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert second.tomorrow_plan == []
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_short_ack_confirms_pending_report_submission(monkeypatch):
    existing = _report(
        today_work=["\u5904\u7406\u5408\u540c\u5ba1\u6838"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["\u7ee7\u7eed\u8ddf\u8fdb\u5ba1\u6279"],
        section_status={"today_work": True, "problems": True, "tomorrow_plan": True, "problems_acknowledged_empty": True},
        status=STATUS_PENDING_CONFIRMATION,
        completeness_score=1.0,
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u55ef", source="test")

    assert result.status == STATUS_COMPLETED
    assert result.confirmed_by_user is True
    assert result.reply_kind == "confirmed"


@pytest.mark.asyncio
async def test_structured_two_field_input_records_plan_and_risk_without_agent(monkeypatch):
    store = _install_store(monkeypatch, None)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u660e\u65e5\u8ba1\u5212\uff1a\u7ee7\u7eed\u5f00\u53d1\n\u98ce\u9669\uff1a\u65e0",
        source="test",
    )

    assert result.report_saved is True
    assert result.timings["decision_route_source"] == "direct_rule"
    assert result.timings["decision_route_branch"] == "direct_structured_multi_field_report"
    assert store["report"].today_work == []
    assert store["report"].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert store["report"].tomorrow_plan == ["\u7ee7\u7eed\u5f00\u53d1"]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_noise_input_is_rejected_before_agent_and_database_write(monkeypatch):
    store = _install_store(monkeypatch, None)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="%PDF-1.4 % 1 0 obj << /Title (Sample) >> endobj",
        source="test",
    )

    assert result.report_saved is False
    assert result.reply_kind == "invalid_report_noise"
    assert store["report"] is None
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_short_no_problem_reply_records_risk_none_for_current_risk_slot(monkeypatch):
    existing = _report(today_work=["\u5b8c\u6210\u63a5\u53e3\u8054\u8c03"], problems=[], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u6ca1\u6709", source="test")

    assert result.report_saved is True
    assert store["report"].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_compound_confirmation_appends_inline_content_in_service_flow(monkeypatch):
    existing = _report(
        today_work=["\u505a\u4e86\u6027\u80fd\u4f18\u5316"],
        problems=[],
        tomorrow_plan=[],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_append_target_confirmation",
                "operation": "append",
                "target_field": "today_work",
                "context": {},
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u5bf9\uff0c\u8fd8\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a",
        source="test",
    )

    assert result.report_saved is True
    assert store["report"].today_work == ["\u505a\u4e86\u6027\u80fd\u4f18\u5316", "\u8fd8\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a"]
    assert "_pending_interaction" not in store["report"].section_status
    assert service.report_agent.calls == []

@pytest.mark.asyncio
async def test_explicit_confirm_submit_does_not_become_tomorrow_plan_when_incomplete(monkeypatch):
    existing = _report(
        today_work=["contract review"],
        problems=["no risk"],
        tomorrow_plan=[],
        section_status={"problems_acknowledged_empty": True},
        status=STATUS_COLLECTING,
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u786e\u8ba4\u63d0\u4ea4", source="test")

    assert result.reply_kind == "confirm_but_incomplete"
    assert store["report"].tomorrow_plan == []
    assert "\u786e\u8ba4\u63d0\u4ea4" not in store["report"].tomorrow_plan
    assert "\u660e\u65e5\u8ba1\u5212" in result.message


@pytest.mark.asyncio
async def test_explicit_confirm_submit_submits_pending_report(monkeypatch):
    existing = _report(
        today_work=["contract review"],
        problems=["no risk"],
        tomorrow_plan=["continue contract follow-up"],
        section_status={"problems_acknowledged_empty": True},
        status=STATUS_PENDING_CONFIRMATION,
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u786e\u8ba4\u63d0\u4ea4", source="test")

    assert result.reply_kind == "confirmed"
    assert store["report"].status == STATUS_COMPLETED
    assert store["report"].tomorrow_plan == ["continue contract follow-up"]


@pytest.mark.asyncio
async def test_date_reassignment_short_date_moves_current_draft_to_target(monkeypatch):
    current_date = date(2026, 6, 25)
    target_date = date(2026, 6, 24)
    current = _report(
        report_date=current_date,
        today_work=["current draft work"],
        problems=["no risk"],
        tomorrow_plan=["current draft plan"],
        status=STATUS_PENDING_CONFIRMATION,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 25, 8, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u8fd9\u662f24\u53f7\u7684", source="test")

    assert result.report_date == target_date
    assert result.reply_kind == "date_reassigned"
    assert store[target_date].today_work == ["current draft work"]
    assert store[target_date].tomorrow_plan == ["current draft plan"]
    assert store[current_date].today_work == []
    assert store[current_date].tomorrow_plan == []
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_date_reassignment_short_date_after_cutoff_is_blocked(monkeypatch):
    current_date = date(2026, 6, 25)
    target_date = date(2026, 6, 24)
    current = _report(
        report_date=current_date,
        today_work=["current draft work"],
        problems=["no risk"],
        tomorrow_plan=["current draft plan"],
        status=STATUS_PENDING_CONFIRMATION,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 25, 10, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u8fd9\u662f24\u53f7\u7684", source="test")

    assert result.report_date == current_date
    assert result.reply_kind == "previous_report_cutoff"
    assert target_date not in store
    assert store[current_date].today_work == ["current draft work"]
    assert store[current_date].tomorrow_plan == ["current draft plan"]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_dated_report_edit_entry_uses_target_date_not_today_label(monkeypatch):
    current_date = date(2026, 6, 25)
    target_date = date(2026, 6, 24)
    current = _report(report_date=current_date, today_work=["current day work"], problems=["no risk"], tomorrow_plan=["current plan"])
    target = _report(report_date=target_date, today_work=["target day work"], problems=["no risk"], tomorrow_plan=["target plan"])
    store = _install_multiday_store(monkeypatch, {current_date: current, target_date: target})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 25, 8, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u4fee\u653924\u53f7\u7684\u65e5\u62a5", source="test")

    assert result.report_date == target_date
    assert "2026-06-24" in result.message
    assert "2026-06-25" not in result.message
    assert "\u4eca\u65e5\u65e5\u62a5" not in result.message
    pending = store[target_date].section_status["_pending_interaction"]
    assert pending["context"]["target_date"] == "2026-06-24"
    assert store[current_date].today_work == ["current day work"]


@pytest.mark.asyncio
async def test_dated_report_action_confirmation_enters_historical_edit_flow(monkeypatch):
    current_date = date(2026, 6, 25)
    target_date = date(2026, 6, 24)
    current = _report(
        report_date=current_date,
        today_work=["current day work"],
        problems=["current risk"],
        tomorrow_plan=["current plan"],
        section_status={
            "_pending_interaction": {
                "type": "awaiting_dated_report_action",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-24",
                    "requested_action": "modify_report",
                    "current_report": {
                        "today_work": ["old historical work"],
                        "problems": ["old historical risk"],
                        "tomorrow_plan": ["old historical plan"],
                    },
                },
            }
        },
    )
    historical = _report(
        report_date=target_date,
        today_work=["old historical work"],
        problems=["old historical risk"],
        tomorrow_plan=["old historical plan"],
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, target_date: historical})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u662f\u7684", source="test")

    assert result.report_saved is True
    assert "2026-06-24" in result.message
    assert "\u8fd9\u53e5\u6211\u5148\u4e0d\u8bb0\u5165" not in result.message
    pending = store[current_date].section_status["_pending_interaction"]
    assert pending["type"] == "historical_report_edit_flow"
    assert pending["context"]["target_date"] == "2026-06-24"
    assert store[current_date].today_work == ["current day work"]
    assert store[target_date].today_work == ["old historical work"]
    assert not service.report_agent.calls


@pytest.mark.asyncio
async def test_current_report_edit_flow_full_snapshot_replaces_sections_without_append(monkeypatch):
    existing = _report(
        today_work=["old work 1", "old work 2"],
        problems=["old risk"],
        tomorrow_plan=["old plan"],
        section_status={
            "_pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-16",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["old work 1", "old work 2"],
                        "problems": ["old risk"],
                        "tomorrow_plan": ["old plan"],
                    },
                },
            }
        },
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    ask = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u4eca\u65e5\u5b8c\u6210\uff1aA\n\u660e\u65e5\u8ba1\u5212\uff1aB\n\u98ce\u9669\uff1a\u65e0",
        source="test",
    )
    done = await service.submit_text(FakeSession(), user=_user(), raw_input="\u786e\u8ba4", source="test")

    assert ask.report_saved is True
    assert done.report_saved is True
    assert store["report"].today_work == ["A"]
    assert store["report"].tomorrow_plan == ["B"]
    assert store["report"].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert "old work" not in "".join(store["report"].today_work)


@pytest.mark.asyncio
async def test_historical_report_edit_flow_full_snapshot_replaces_target_report(monkeypatch):
    target_date = date(2026, 6, 24)
    current_date = date(2026, 6, 25)
    current = _report(
        report_date=current_date,
        today_work=["current day work"],
        problems=["current risk"],
        tomorrow_plan=["current plan"],
        section_status={
            "_pending_interaction": {
                "type": "historical_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-24",
                    "stage": "awaiting_edit_instruction",
                    "current_report": {
                        "today_work": ["old historical work"],
                        "problems": ["old historical risk"],
                        "tomorrow_plan": ["old historical plan"],
                    },
                },
            }
        },
    )
    historical = _report(
        report_date=target_date,
        today_work=["old historical work"],
        problems=["old historical risk"],
        tomorrow_plan=["old historical plan"],
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, target_date: historical})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    ask = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u4eca\u65e5\u5b8c\u6210\uff1aA\n\u95ee\u9898/\u98ce\u9669\uff1a\u65e0\n\u660e\u65e5\u8ba1\u5212\uff1aB",
        source="test",
    )

    assert ask.report_saved is True
    assert "2026-06-24" in ask.message
    assert store[target_date].today_work == ["old historical work"]

    done = await service.submit_text(FakeSession(), user=_user(), raw_input="\u786e\u8ba4", source="test")

    assert done.report_saved is True
    assert store[target_date].today_work == ["A"]
    assert store[target_date].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert store[target_date].tomorrow_plan == ["B"]
    assert store[current_date].today_work == ["current day work"]
    assert store[current_date].problems == ["current risk"]
    assert store[current_date].tomorrow_plan == ["current plan"]



@pytest.mark.asyncio
async def test_explicit_ordinal_rewrite_keeps_item_boundaries_when_agent_rewrites_field(monkeypatch):
    replacement = "\u5904\u7406\u7efc\u5408\u7ba1\u7406\u90e8\u65e5\u5e38\u884c\u653f\u652f\u6301"
    existing = _report(
        today_work=[
            "work 1",
            "work 2",
            "work 3",
            "work 4",
            "work 5",
            "work 6",
            "old work 7",
        ],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["plan 1"],
        section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(
                        type="replace_field",
                        field="today_work",
                        items=[f"wrong split {index}" for index in range(1, 11)],
                    )
                ],
                reason="simulated bad LLM whole-field rewrite for one ordinal edit",
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="\u7b2c\u4e03\u6761\u6539\u6210" + replacement,
        source="test",
    )

    assert service.report_agent.calls
    assert result.report_saved is True
    assert len(store["report"].today_work) == 7
    assert store["report"].today_work[:6] == existing.today_work[:6]
    assert store["report"].today_work[6] == replacement
    assert "wrong split" not in "".join(store["report"].today_work)


@pytest.mark.asyncio
async def test_business_problem_word_in_today_context_stays_today_work(monkeypatch):
    existing = _report(today_work=["contract review"], problems=[], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="append_items", field="today_work", items=["\u666f\u89c2\u65bd\u5de5\u5408\u540c\u5f02\u5e38\u6d41\u7a0b\u6c9f\u901a"])],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u666f\u89c2\u65bd\u5de5\u5408\u540c\u5f02\u5e38\u6d41\u7a0b\u6c9f\u901a", source="test")

    assert result.report_saved is True
    assert "\u666f\u89c2\u65bd\u5de5\u5408\u540c\u5f02\u5e38\u6d41\u7a0b\u6c9f\u901a" in store["report"].today_work
    assert "\u666f\u89c2\u65bd\u5de5\u5408\u540c\u5f02\u5e38\u6d41\u7a0b\u6c9f\u901a" not in store["report"].problems


@pytest.mark.asyncio
async def test_no_problem_colloquial_reply_normalizes_to_no_risk(monkeypatch):
    existing = _report(today_work=["contract review"], problems=[], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="\u6ca1\u78b0\u5230\u4ec0\u4e48\u95ee\u9898", source="test")

    assert result.report_saved is True
    assert store["report"].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert "\u6ca1\u78b0\u5230\u4ec0\u4e48\u95ee\u9898" not in store["report"].problems
@pytest.mark.asyncio
async def test_no_problem_in_full_sentence_does_not_skip_work_and_plan(monkeypatch):
    store = _install_store(monkeypatch, None)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(type="replace_field", field="today_work", items=["处理合同审核、印章流转、服务器方案沟通"]),
                    AgentAction(type="replace_field", field="problems", items=["暂无明显问题"]),
                    AgentAction(type="replace_field", field="tomorrow_plan", items=["继续推进"]),
                ],
                reason="full sentence report with no-risk tail",
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="怎么说呢今天其实就处理了三件事：合同审核、印章流转、服务器方案沟通，暂时没风险，明天继续推进。",
        source="test",
    )

    assert len(service.report_agent.calls) == 1
    assert result.report_saved is True
    assert store["report"].today_work == ["处理合同审核", "印章流转", "服务器方案沟通"]
    assert store["report"].problems == ["暂无明显问题"]
    assert store["report"].tomorrow_plan == ["继续推进"]


@pytest.mark.asyncio
async def test_replace_current_report_intent_clears_old_problem_when_agent_omits_problem(monkeypatch):
    existing = _report(
        today_work=["吃了手抓饼"],
        problems=["蛋没给全"],
        tomorrow_plan=["明天去苏州吃手抓饼"],
        status=STATUS_PENDING_CONFIRMATION,
        completeness_score=1.0,
    )
    store = _install_store(monkeypatch, existing)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(type="replace_field", field="today_work", items=["审了三个合同"]),
                    AgentAction(type="replace_field", field="tomorrow_plan", items=["明天去南京开庭"]),
                ],
                reason="simulated model omitted no-problem field during replacement",
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="算了算了，跟你实话实说吧，今天审了三个合同，然后没啥问题，明天去南京开庭",
        source="test",
    )

    assert result.report_saved is True
    assert store["report"].today_work == ["审了三个合同"]
    assert store["report"].problems == ["暂无明显问题"]
    assert store["report"].tomorrow_plan == ["明天去南京开庭"]
    all_values = store["report"].today_work + store["report"].problems + store["report"].tomorrow_plan
    assert not any("手抓饼" in value or "蛋没" in value or "苏州" in value for value in all_values)


@pytest.mark.asyncio
async def test_recent_history_query_copy_all_to_today(monkeypatch):
    current = _report(report_date=date(2026, 6, 16), today_work=[], problems=[], tomorrow_plan=[])
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["昨天处理合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["今天继续跟进"],
        status=STATUS_COMPLETED,
    )
    store = _install_store(monkeypatch, current, previous=previous)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 17, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="system_action",
                confidence="high",
                should_write=False,
                actions=[AgentAction(type="no_op")],
                reply_to_user="请说明要复制什么内容。",
            )
        ]
    )

    query = await service.submit_text(FakeSession(), user=_user(), raw_input="昨天日报发我下", source="test")
    assert "2026-06-15" in query.message

    copied = await service.submit_text(FakeSession(), user=_user(), raw_input="全部复制", source="test")

    assert copied.report_saved is True
    assert copied.reply_kind == "recent_report_copy_to_today"
    assert store["report"].today_work == ["昨天处理合同"]
    assert store["report"].problems == ["暂无明显问题"]
    assert store["report"].tomorrow_plan == ["今天继续跟进"]
    assert service.report_agent.calls == []



@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input",
    [
        "\u628a\u6628\u5929\uff082026-06-25\uff09\u7684\u65e5\u62a5\u5b8c\u6574\u590d\u5236\u5230\u4eca\u5929\uff0c\u5148\u4f5c\u4e3a\u4eca\u5929\u8349\u7a3f\uff0c\u4e0d\u8981\u63d0\u4ea4\u3002",
        "\u590d\u5236\u6628\u5929\u65e5\u62a5\u5230\u4eca\u5929\u3002",
        "\u590d\u5236\u6628\u5929\u7684\u65e5\u62a5",
        "\u628a\u6628\u5929\u65e5\u62a5\u539f\u6837\u4f5c\u4e3a\u4eca\u5929\u8349\u7a3f\u3002",
        "\u7528\u6628\u5929\u65e5\u62a5\u751f\u6210\u4eca\u5929\u8349\u7a3f\u3002",
        "\u628a\u6628\u5929\u65e5\u62a5\u5e26\u5230\u4eca\u5929\u3002",
    ],
)
async def test_direct_copy_yesterday_report_to_today_after_cutoff(monkeypatch, raw_input):
    current_date = date(2026, 6, 26)
    previous_date = date(2026, 6, 25)
    current = _report(report_date=current_date, today_work=[], problems=[], tomorrow_plan=[])
    previous = _report(
        report_date=previous_date,
        today_work=["\u6628\u5929\u5904\u7406\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["\u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb"],
        status=STATUS_COMPLETED,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, previous_date: previous})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 26, 9, 30))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    copied = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test")

    assert copied.report_saved is True
    assert copied.reply_kind == "recent_report_copy_to_today"
    assert copied.report_date == current_date
    assert store[current_date].today_work == ["\u6628\u5929\u5904\u7406\u5408\u540c"]
    assert store[current_date].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert store[current_date].tomorrow_plan == ["\u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb"]
    assert "\u4eca\u65e5\u5de5\u4f5c\uff1a\n1. \u6628\u5929\u5904\u7406\u5408\u540c" in copied.message
    assert "\u95ee\u9898/\u98ce\u9669\uff1a\n1. \u6682\u65e0\u660e\u663e\u95ee\u9898" in copied.message
    assert "\u660e\u65e5\u8ba1\u5212\uff1a\n1. \u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb" in copied.message
    assert service.report_agent.calls == []



@pytest.mark.asyncio
async def test_direct_whole_copy_yesterday_report_overwrites_existing_today(monkeypatch):
    current_date = date(2026, 6, 26)
    previous_date = date(2026, 6, 25)
    current = _report(
        report_date=current_date,
        today_work=["\u65e7\u7684\u4eca\u65e5\u5de5\u4f5c"],
        problems=["\u65e7\u7684\u95ee\u9898"],
        tomorrow_plan=["\u65e7\u7684\u660e\u65e5\u8ba1\u5212"],
    )
    previous = _report(
        report_date=previous_date,
        today_work=["\u6628\u5929\u5904\u7406\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["\u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb"],
        status=STATUS_COMPLETED,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, previous_date: previous})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 26, 9, 45))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    copied = await service.submit_text(FakeSession(), user=_user(), raw_input="\u6574\u7bc7\u590d\u5236\u6628\u5929\u65e5\u62a5\u5230\u4eca\u5929", source="test")

    assert copied.report_saved is True
    assert copied.reply_kind == "recent_report_copy_to_today"
    assert store[current_date].today_work == ["\u6628\u5929\u5904\u7406\u5408\u540c"]
    assert store[current_date].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert store[current_date].tomorrow_plan == ["\u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb"]
    assert "\u65e7\u7684" not in "\n".join(store[current_date].today_work + store[current_date].problems + store[current_date].tomorrow_plan)
    assert service.report_agent.calls == []




@pytest.mark.asyncio
@pytest.mark.parametrize("raw_input", ["复制前天日报", "复制前天内容", "把前日的内容复制到今天"])
async def test_direct_copy_day_before_yesterday_report_to_today(monkeypatch, raw_input):
    current_date = date(2026, 6, 26)
    source_date = date(2026, 6, 24)
    current = _report(report_date=current_date, today_work=[], problems=[], tomorrow_plan=[])
    source_report = _report(
        report_date=source_date,
        today_work=["\u524d\u5929\u5de5\u4f5cA", "\u524d\u5929\u5de5\u4f5cB"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["\u540e\u7eed\u8ddf\u8fdb"],
        status=STATUS_COMPLETED,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, source_date: source_report})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 26, 10, 10))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    copied = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test")

    assert copied.report_saved is True
    assert copied.reply_kind == "recent_report_copy_to_today"
    assert store[current_date].today_work == ["\u524d\u5929\u5de5\u4f5cA", "\u524d\u5929\u5de5\u4f5cB"]
    assert store[current_date].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert store[current_date].tomorrow_plan == ["\u540e\u7eed\u8ddf\u8fdb"]
    assert "\u4eca\u65e5\u5de5\u4f5c\uff1a\n1. \u524d\u5929\u5de5\u4f5cA\n2. \u524d\u5929\u5de5\u4f5cB" in copied.message
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_direct_copy_dated_content_to_today(monkeypatch):
    current_date = date(2026, 6, 26)
    source_date = date(2026, 6, 24)
    current = _report(report_date=current_date, today_work=["旧内容"], problems=["旧问题"], tomorrow_plan=["旧计划"])
    source_report = _report(
        report_date=source_date,
        today_work=["日报系统的新一轮优化", "整理被告数据", "完成中建合同线上确认"],
        problems=["没碰到什么问题"],
        tomorrow_plan=["明天等额度刷新后开始做日报系统的案件进展与出差协同俩个outbox"],
        status=STATUS_COMPLETED,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, source_date: source_report})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 26, 10, 10))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    copied = await service.submit_text(FakeSession(), user=_user(), raw_input="复制6月24日内容", source="test")

    assert copied.report_saved is True
    assert copied.reply_kind == "recent_report_copy_to_today"
    assert store[current_date].today_work == source_report.today_work
    assert store[current_date].problems == source_report.problems
    assert store[current_date].tomorrow_plan == source_report.tomorrow_plan
    assert "旧" not in "\n".join(store[current_date].today_work + store[current_date].problems + store[current_date].tomorrow_plan)
    assert "今日工作：\n1. 日报系统的新一轮优化" in copied.message
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_current_labeled_report_paste_replaces_today_not_reference(monkeypatch):
    current_date = date(2026, 6, 26)
    current = _report(
        report_date=current_date,
        today_work=["旧的今日工作"],
        problems=["旧的问题"],
        tomorrow_plan=["旧的明日计划"],
        section_status={"_recent_report_context": {"report_date": "2026-06-25"}},
    )
    store = _install_multiday_store(monkeypatch, {current_date: current})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 26, 21, 20))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])
    raw_input = """这是今天的，不是昨天的。
**【日期】** 2026-06-26
**【今日工作】**
1. 日常用印流程审批
2. 现场用印审核
3. 电子章用印
4. 未归档合同催收
5. 明日半年度会议后晚餐的订餐工作
**【问题/风险】**
无
**【明日计划】**
1. 日常用印流程审批
2. 现场用印审核
3. 电子章用印
4. 未归档合同催收
5. 用印事宜咨询答复
6. 参加部门会议，编制会议纪要
"""

    result = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test")

    assert result.report_saved is True
    assert result.reply_kind != "previous_report_cutoff"
    assert store[current_date].today_work == [
        "日常用印流程审批",
        "现场用印审核",
        "电子章用印",
        "未归档合同催收",
        "明日半年度会议后晚餐的订餐工作",
    ]
    assert store[current_date].problems == ["暂无明显问题"]
    assert store[current_date].tomorrow_plan == [
        "日常用印流程审批",
        "现场用印审核",
        "电子章用印",
        "未归档合同催收",
        "用印事宜咨询答复",
        "参加部门会议，编制会议纪要",
    ]
    assert "_reference_report_context" not in store[current_date].section_status
    assert "明日计划：\n1. 日常用印流程审批" in result.message
    assert "6. 参加部门会议，编制会议纪要" in result.message
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_current_dated_template_matching_report_date_replaces_today(monkeypatch):
    current_date = date(2026, 6, 26)
    current = _report(report_date=current_date, today_work=[], problems=[], tomorrow_plan=[])
    store = _install_multiday_store(monkeypatch, {current_date: current})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])
    raw_input = """当前填报日期：2026-06-26
**【日期】** 2026-06-26
**【今日工作】**
1. 日常用印流程审批
2. 现场用印审核
**【问题/风险】** 无
**【明日计划】**
1. 用印事宜咨询答复
2. 参加部门会议，编制会议纪要
"""

    result = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test")

    assert result.report_saved is True
    assert store[current_date].today_work == ["日常用印流程审批", "现场用印审核"]
    assert store[current_date].problems == ["暂无明显问题"]
    assert store[current_date].tomorrow_plan == ["用印事宜咨询答复", "参加部门会议，编制会议纪要"]
    assert "_reference_report_context" not in store[current_date].section_status
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_history_query_uses_explicit_report_date_as_relative_anchor(monkeypatch):
    current_date = date(2026, 6, 26)
    server_today = date(2026, 6, 27)
    previous_date = date(2026, 6, 25)
    current = _report(report_date=current_date, today_work=[], problems=[], tomorrow_plan=[])
    previous = _report(
        report_date=previous_date,
        today_work=["昨天工作A"],
        problems=["暂无明显问题"],
        tomorrow_plan=["昨天计划A"],
        status=STATUS_COMPLETED,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, previous_date: previous})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: server_today)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 27, 2, 30))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="昨天日报发我看下",
        source="test",
        report_date=current_date,
    )

    assert result.report_saved is False
    assert result.reply_kind == "history_query"
    assert "2026-06-25" in result.message
    assert "昨天工作A" in result.message
    assert store[current_date].section_status["_recent_report_context"]["report_date"] == "2026-06-25"
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_whole_copy_splits_multiline_source_sections_and_numbers_reply(monkeypatch):
    current_date = date(2026, 6, 26)
    previous_date = date(2026, 6, 25)
    current = _report(report_date=current_date, today_work=[], problems=[], tomorrow_plan=[])
    previous = _report(
        report_date=previous_date,
        today_work=["1. \u5ba1\u6838\u5408\u540cA\n2. \u5904\u7406\u7528\u5370B"],
        problems=["1\u3001\u98ce\u9669A\n2\u3001\u98ce\u9669B"],
        tomorrow_plan=["- \u8ddf\u8fdbC\n- \u5f52\u6863D"],
        status=STATUS_COMPLETED,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, previous_date: previous})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 26, 9, 45))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    copied = await service.submit_text(FakeSession(), user=_user(), raw_input="\u6574\u7bc7\u590d\u5236\u6628\u5929\u65e5\u62a5\u5230\u4eca\u5929", source="test")

    assert copied.report_saved is True
    assert store[current_date].today_work == ["\u5ba1\u6838\u5408\u540cA", "\u5904\u7406\u7528\u5370B"]
    assert store[current_date].problems == ["\u98ce\u9669A", "\u98ce\u9669B"]
    assert store[current_date].tomorrow_plan == ["\u8ddf\u8fdbC", "\u5f52\u6863D"]
    assert "\u4eca\u65e5\u5de5\u4f5c\uff1a\n1. \u5ba1\u6838\u5408\u540cA\n2. \u5904\u7406\u7528\u5370B" in copied.message
    assert "\u95ee\u9898/\u98ce\u9669\uff1a\n1. \u98ce\u9669A\n2. \u98ce\u9669B" in copied.message
    assert "\u660e\u65e5\u8ba1\u5212\uff1a\n1. \u8ddf\u8fdbC\n2. \u5f52\u6863D" in copied.message


@pytest.mark.asyncio
async def test_whole_copy_splits_space_separated_source_sections(monkeypatch):
    current_date = date(2026, 6, 26)
    previous_date = date(2026, 6, 25)
    current = _report(report_date=current_date, today_work=[], problems=[], tomorrow_plan=[])
    previous = _report(
        report_date=previous_date,
        today_work=["\u5904\u7406\u65e5\u62a5\u586b\u5199bug    \u5b8c\u6210\u7528\u5370\u5ba1\u6838    \u6574\u7406\u5f52\u6863"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["\u7ee7\u7eed\u4fee\u590d\u586b\u5199\u95ee\u9898    \u8ddf\u8fdb\u7528\u5370\u5ba1\u6838"],
        status=STATUS_COMPLETED,
    )
    store = _install_multiday_store(monkeypatch, {current_date: current, previous_date: previous})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 26, 10, 10))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    copied = await service.submit_text(FakeSession(), user=_user(), raw_input="\u590d\u5236\u6628\u5929\u65e5\u62a5", source="test")

    assert copied.report_saved is True
    assert store[current_date].today_work == ["\u5904\u7406\u65e5\u62a5\u586b\u5199bug", "\u5b8c\u6210\u7528\u5370\u5ba1\u6838", "\u6574\u7406\u5f52\u6863"]
    assert store[current_date].tomorrow_plan == ["\u7ee7\u7eed\u4fee\u590d\u586b\u5199\u95ee\u9898", "\u8ddf\u8fdb\u7528\u5370\u5ba1\u6838"]
    assert "\u4eca\u65e5\u5de5\u4f5c\uff1a\n1. \u5904\u7406\u65e5\u62a5\u586b\u5199bug\n2. \u5b8c\u6210\u7528\u5370\u5ba1\u6838\n3. \u6574\u7406\u5f52\u6863" in copied.message


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_reply", ["\u6574\u7bc7\u590d\u5236", "\u628a\u8fd9\u4efd\u5e26\u5230\u4eca\u5929"])
async def test_recent_history_query_whole_copy_overwrites_existing_today(monkeypatch, copy_reply):
    current = _report(report_date=date(2026, 6, 16), today_work=["\u65e7\u5de5\u4f5c"], problems=["\u65e7\u95ee\u9898"], tomorrow_plan=["\u65e7\u8ba1\u5212"])
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["\u6628\u5929\u5904\u7406\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["\u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb"],
        status=STATUS_COMPLETED,
    )
    store = _install_store(monkeypatch, current, previous=previous)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 17, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    query = await service.submit_text(FakeSession(), user=_user(), raw_input="\u6628\u5929\u65e5\u62a5\u53d1\u6211\u4e0b", source="test")
    copied = await service.submit_text(FakeSession(), user=_user(), raw_input=copy_reply, source="test")

    assert "2026-06-15" in query.message
    assert copied.report_saved is True
    assert copied.reply_kind == "recent_report_copy_to_today"
    assert store["report"].today_work == ["\u6628\u5929\u5904\u7406\u5408\u540c"]
    assert store["report"].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert store["report"].tomorrow_plan == ["\u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb"]
    assert service.report_agent.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_reply", ["\u5168\u90e8\u8986\u76d6", "\u8986\u76d6"])
async def test_recent_history_query_cover_reply_whole_copies_context_report(monkeypatch, copy_reply):
    current = _report(report_date=date(2026, 6, 16), today_work=["\u65e7\u5de5\u4f5c"], problems=["\u65e7\u95ee\u9898"], tomorrow_plan=["\u65e7\u8ba1\u5212"])
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["\u6628\u5929\u5904\u7406\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=["\u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb"],
        status=STATUS_COMPLETED,
    )
    store = _install_store(monkeypatch, current, previous=previous)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 17, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    await service.submit_text(FakeSession(), user=_user(), raw_input="\u6628\u5929\u65e5\u62a5\u53d1\u6211\u4e0b", source="test")
    copied = await service.submit_text(FakeSession(), user=_user(), raw_input=copy_reply, source="test")

    assert copied.report_saved is True
    assert copied.reply_kind == "recent_report_copy_to_today"
    assert store["report"].today_work == ["\u6628\u5929\u5904\u7406\u5408\u540c"]
    assert store["report"].problems == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert store["report"].tomorrow_plan == ["\u4eca\u5929\u7ee7\u7eed\u8ddf\u8fdb"]
    assert "\u5df2\u6574\u7bc7\u590d\u5236" in copied.message
    assert service.report_agent.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input",
    [
        "\u6628\u5929\u65e5\u62a5\u4f5c\u4e3a\u53c2\u8003\uff0c\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838\uff0c\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u3002",
        "2026-06-25\u7684\u65e5\u62a5\u4f5c\u4e3a\u53c2\u8003\uff0c\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838\uff0c\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u3002",
    ],
)
async def test_previous_date_reference_alone_does_not_trigger_history_cutoff(monkeypatch, raw_input):
    current_date = date(2026, 6, 26)
    previous_date = date(2026, 6, 25)
    current = _report(report_date=current_date, today_work=[], problems=[], tomorrow_plan=[])
    previous = _report(report_date=previous_date, today_work=["\u6628\u5929\u5de5\u4f5c"], problems=[], tomorrow_plan=[])
    store = _install_multiday_store(monkeypatch, {current_date: current, previous_date: previous})
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: current_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 26, 9, 45))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(type="replace_field", field="today_work", items=["\u5b8c\u6210\u5408\u540c\u5ba1\u6838"]),
                    AgentAction(type="replace_field", field="tomorrow_plan", items=["\u7ee7\u7eed\u8ddf\u8fdb"]),
                ],
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input=raw_input, source="test")

    assert result.report_saved is True
    assert result.reply_kind != "previous_report_cutoff"
    assert store[current_date].today_work == ["\u5b8c\u6210\u5408\u540c\u5ba1\u6838"]
    assert store[current_date].tomorrow_plan == ["\u7ee7\u7eed\u8ddf\u8fdb"]
    assert store[previous_date].today_work == ["\u6628\u5929\u5de5\u4f5c"]


@pytest.mark.asyncio
async def test_bare_yesterday_without_recent_query_context_does_not_write(monkeypatch):
    current = _report(report_date=date(2026, 6, 16), today_work=[], problems=[], tomorrow_plan=[])
    previous = _report(
        report_date=date(2026, 6, 15),
        today_work=["昨天处理合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["今天继续跟进"],
        status=STATUS_COMPLETED,
    )
    store = _install_store(monkeypatch, current, previous=previous)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 17, 0))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[AgentAction(type="complete_all_previous_plan_items", source_section="tomorrow_plan", target_section="today_work")],
                reason="bad bare yesterday rollover",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="昨天的", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "recent_report_context_missing"
    assert store["report"].today_work == []
    assert service.report_agent.calls == []


@pytest.mark.asyncio
async def test_yesterday_content_before_nine_targets_previous_report(monkeypatch):
    store = _install_store(monkeypatch, None)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 8, 30))
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(type="replace_field", field="today_work", items=["审核合同"]),
                    AgentAction(type="replace_field", field="problems", items=["暂无明显问题"]),
                ],
                reason="previous day content before cutoff",
            )
        ]
    )

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="昨天审核了合同，没问题，今天继续。", source="test")

    assert result.report_saved is True
    assert result.report_date == date(2026, 6, 17)
    assert store["report"].report_date == date(2026, 6, 17)
    assert store["report"].today_work == ["审核合同"]


@pytest.mark.asyncio
async def test_yesterday_content_after_nine_is_blocked(monkeypatch):
    _install_store(monkeypatch, None)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 20, 30))
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="补一下昨天的，昨天整理了材料。", source="test")

    assert result.report_saved is False
    assert result.report_date == date(2026, 6, 18)
    assert "09:00" in result.message


@pytest.mark.asyncio
async def test_yesterday_word_inside_current_report_is_not_cutoff_blocked(monkeypatch):
    store = _install_store(monkeypatch, None)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 20, 30))
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent(
        [
            ActionPlan(
                intent="fill_report",
                confidence="high",
                should_write=True,
                actions=[
                    AgentAction(type="replace_field", field="today_work", items=["复盘昨天的合同问题"]),
                    AgentAction(type="replace_field", field="problems", items=["暂无明显问题"]),
                    AgentAction(type="replace_field", field="tomorrow_plan", items=["明天继续跟进印章"]),
                ],
                reason="current report mentions yesterday as content context",
            )
        ]
    )

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="今天复盘了昨天的合同问题，没有新风险，明天继续跟进印章",
        source="test",
    )

    assert result.report_saved is True
    assert result.report_date == date(2026, 6, 18)
    assert any("复盘" in item and "合同" in item for item in store["report"].today_work)
    assert store["report"].problems == ["暂无明显问题"]
    assert any("印章" in item for item in store["report"].tomorrow_plan)


@pytest.mark.asyncio
async def test_asking_can_modify_yesterday_after_cutoff_does_not_create_pending(monkeypatch):
    existing = _report(today_work=["审核合同"], problems=[], tomorrow_plan=[])
    store = _install_store(monkeypatch, existing)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 20, 30))
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="我现在能改昨天的吗？", source="test")

    assert result.report_saved is False
    assert "09:00" in result.message
    assert "_pending_interaction" not in store["report"].section_status


@pytest.mark.asyncio
async def test_single_action_many_effects_split_into_numbered_work_items(monkeypatch):
    store = _install_store(monkeypatch, None)
    service = DailyReportService(_settings(), FakeExtractor())
    service.report_agent = FakeAgent([])

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="今天优化了AI发送逻辑：1. 填报日期更清楚了 2. 9点规则收紧了 3. 编辑能力稳定了一轮 4. 自动提交逻辑更保守 5. 长文本枚举归属修了 6. LLM使用更稳了 7. 影子记忆系统",
        source="test",
        report_date=date(2026, 6, 19),
    )

    assert result.report_saved is True
    assert len(store["report"].today_work) == 7
    assert store["report"].today_work[0] == "填报日期更清楚了"
    assert "影子记忆" in store["report"].today_work[-1]
