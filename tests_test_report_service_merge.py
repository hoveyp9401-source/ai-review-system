from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.llm.extractor import LLMCallResult
from app.schemas import DailyInputIntentDecision, DraftDecision, StructuredDailyReport
from app.services import report_service
from app.services.report_service import DailyReportService
from app.services.state_machine import (
    CONFIRMATION_NONE,
    CONFIRMATION_USER_CONFIRMED,
    STATUS_COLLECTING,
    STATUS_COMPLETED,
    STATUS_PENDING_CONFIRMATION,
)


class FakeExtractor:
    model = "fake"

    def __init__(self, outputs, intent_outputs=None):
        self.outputs = list(outputs)
        self.intent_outputs = list(intent_outputs or [])
        self.client = SimpleNamespace(model="fake")

    async def extract(self, raw_input):
        return self.outputs.pop(0)

    async def decide_intent(self, *, raw_input, context):
        if self.intent_outputs:
            return self.intent_outputs.pop(0)
        return DailyInputIntentDecision(intent="continue_collecting", confidence=0.8)


class FakeDraftExtractor(FakeExtractor):
    def __init__(self, draft_outputs):
        super().__init__([])
        self.draft_outputs = list(draft_outputs)
        self.contexts = []

    async def decide_draft_with_meta(self, *, raw_input, context):
        self.contexts.append(context)
        return LLMCallResult(payload=self.draft_outputs.pop(0), meta={"model": "fake", "thinking": False})


class FakeSession:
    def add(self, obj):
        self.obj = obj

    async def flush(self):
        return None


def _make_report(**overrides):
    values = {
        "id": uuid4(),
        "report_date": date(2026, 6, 14),
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


@pytest.fixture(autouse=True)
def _freeze_before_daily_report_lock(monkeypatch):
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 14, 8, 0))


@pytest.mark.asyncio
async def test_fragmented_report_enters_pending_confirmation(monkeypatch):
    stored = {"report": None}
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        report = _make_report(
            id=stored["report"].id if stored["report"] else uuid4(),
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
            submitted_at=kwargs["received_at"] if kwargs["status"] == STATUS_COMPLETED else None,
        )
        stored["report"] = report
        return report

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor(
        [
            StructuredDailyReport(today_work=["处理合同审核"], completeness=0.34),
            StructuredDailyReport(today_work=[], problems=[], tomorrow_plan=[], completeness=0.0),
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    first = await service.submit_text(FakeSession(), user=user, raw_input="今天处理合同审核。", source="test")
    second = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="没什么问题，明天继续跟业务确认条款。",
        source="test",
    )

    assert first.status == STATUS_COLLECTING
    assert second.status == STATUS_PENDING_CONFIRMATION
    assert second.problems == ["暂无明显问题"]
    assert second.tomorrow_plan == ["明天继续跟业务确认条款"]
    assert second.quality_warning is None
    assert "请确认一下" in second.message


@pytest.mark.asyncio
async def test_confirm_turns_pending_confirmation_into_completed(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": True,
                "problems_acknowledged_empty": True,
            },
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(
            **{**stored["report"].__dict__, **kwargs, "status": kwargs["status"], "confirmation_type": kwargs["confirmation_type"], "confirmed_by_user": kwargs["confirmed_by_user"], "completeness_score": kwargs["completeness_score"]}
        )
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="确认", source="test")

    assert result.status == STATUS_COMPLETED
    assert result.confirmation_type == CONFIRMATION_USER_CONFIRMED
    assert result.confirmed_by_user is True
    assert "复盘已提交" in result.message


@pytest.mark.asyncio
async def test_confirmation_with_courtesy_suffix_submits_pending_report(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": True,
                "problems_acknowledged_empty": True,
            },
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(
            **{**stored["report"].__dict__, **kwargs, "status": kwargs["status"], "confirmation_type": kwargs["confirmation_type"], "confirmed_by_user": kwargs["confirmed_by_user"], "completeness_score": kwargs["completeness_score"]}
        )
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="可以，谢谢", source="test")

    assert result.status == STATUS_COMPLETED
    assert result.confirmed_by_user is True
    assert result.reply_kind == "confirmed"


@pytest.mark.asyncio
async def test_completed_report_can_be_modified_same_day(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": True,
                "problems_acknowledged_empty": True,
            },
            status=STATUS_COMPLETED,
            confirmation_type="auto_submitted_timeout",
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(
            **{**stored["report"].__dict__, **kwargs, "status": kwargs["status"], "quality_warning": kwargs["quality_warning"], "last_modified_by_user": kwargs["last_modified_by_user"], "last_modified_at": kwargs["last_modified_at"]}
        )
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor([StructuredDailyReport(today_work=[], problems=[], tomorrow_plan=["明天改成继续跟进合同审批"], completeness=0.33)])
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)
    result = await service.submit_text(FakeSession(), user=user, raw_input="明日计划改成继续跟进合同审批。", source="test")

    assert result.status == STATUS_COMPLETED
    assert result.report_saved is True
    assert result.tomorrow_plan == ["继续跟进合同审批"]
    assert result.reply_kind == "updated_completed_report"
    assert "已更新今日复盘" in result.message


@pytest.mark.asyncio
async def test_direct_field_updates_use_fast_path_and_keep_other_fields(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": True,
                "problems_acknowledged_empty": True,
            },
            status=STATUS_COLLECTING,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))

    tomorrow = await service.submit_text(FakeSession(), user=user, raw_input="明日计划改成继续推进合同审批。", source="test")
    assert tomorrow.today_work == ["处理合同审核"]
    assert tomorrow.problems == ["暂无明显问题"]
    assert tomorrow.tomorrow_plan == ["继续推进合同审批"]
    assert tomorrow.timings["llm_extract_seconds"] == 0.0

    problems = await service.submit_text(FakeSession(), user=user, raw_input="问题改成资料不齐。", source="test")
    assert problems.today_work == ["处理合同审核"]
    assert problems.problems == ["资料不齐"]
    assert problems.tomorrow_plan == ["继续推进合同审批"]
    assert problems.timings["llm_extract_seconds"] == 0.0

    today = await service.submit_text(FakeSession(), user=user, raw_input="今天工作改成用印审核，审核了很多用印材料。", source="test")
    assert today.today_work == ["用印审核，审核了很多用印材料"]
    assert today.problems == ["资料不齐"]
    assert today.tomorrow_plan == ["继续推进合同审批"]
    assert today.timings["llm_extract_seconds"] == 0.0


@pytest.mark.asyncio
async def test_casual_chat_is_not_written_into_report(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return None

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="哈哈哈", source="test")

    assert result.report_saved is False
    assert result.report_id is None
    assert "先不记入复盘" in result.message


@pytest.mark.asyncio
async def test_normal_test_reply_is_not_written_into_report(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return None

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="正常", source="test")

    assert result.report_saved is False
    assert result.report_id is None
    assert result.reply_kind == "casual_or_invalid"


@pytest.mark.asyncio
async def test_courtesy_reply_is_not_written_into_report(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            section_status={"today_work": True, "problems": False, "tomorrow_plan": False},
            status=STATUS_COLLECTING,
            completeness_score=0.34,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="收到，谢谢", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "courtesy_reply"
    assert result.today_work == ["处理合同审核"]
    assert result.problems == []


@pytest.mark.asyncio
async def test_postpone_reply_is_not_written_into_report(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": False,
                "problems_acknowledged_empty": True,
            },
            status=STATUS_COLLECTING,
            completeness_score=0.67,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="额别烦我，晚点再写", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "postpone_reply"
    assert result.tomorrow_plan == []
    assert "晚点" in result.message


@pytest.mark.asyncio
async def test_clear_current_report_clears_existing_draft_without_llm(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["吃了手抓饼"],
            problems=["蛋没给我加全"],
            tomorrow_plan=["明天去苏州吃手抓饼"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            confirmation_type=CONFIRMATION_NONE,
            confirmed_by_user=False,
            quality_warning="内容较口语化，可能是测试内容",
            completeness_score=1.0,
            raw_input="吃了手抓饼",
            input_fragments=[{"raw_input": "吃了手抓饼"}],
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="再改下吧，先帮我清空。", source="test")

    assert result.report_saved is True
    assert result.reply_kind == "clear_current_report"
    assert result.status == STATUS_COLLECTING
    assert result.completeness_score == 0.0
    assert result.today_work == []
    assert result.problems == []
    assert result.tomorrow_plan == []
    assert result.quality_warning is None
    assert result.confirmed_by_user is False
    assert result.missing_sections == ["today_work", "problems", "tomorrow_plan"]
    assert "已清空当前复盘草稿" in result.message
    assert "手抓饼" not in result.message
    assert stored["report"].raw_input == ""
    assert stored["report"].input_fragments == []


@pytest.mark.asyncio
async def test_collecting_clear_current_report_clears_without_confirmation(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["资料不齐"],
            tomorrow_plan=["继续跟进审批"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_COLLECTING,
            quality_warning="内容较口语化，可能是测试内容",
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="再改下吧，你先帮我清空", source="test")

    assert result.report_saved is True
    assert result.reply_kind == "clear_current_report"
    assert result.status == STATUS_COLLECTING
    assert result.today_work == []
    assert result.problems == []
    assert result.tomorrow_plan == []
    assert result.quality_warning is None
    assert "已清空当前复盘草稿" in result.message
    assert "确认" not in result.message
    assert "再改下吧" not in result.today_work


@pytest.mark.asyncio
async def test_pending_confirmation_clear_current_report_clears_without_confirmation(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="先帮我清空", source="test")

    assert result.report_saved is True
    assert result.status == STATUS_COLLECTING
    assert result.today_work == []
    assert result.problems == []
    assert result.tomorrow_plan == []
    assert "已清空当前复盘草稿" in result.message


@pytest.mark.asyncio
async def test_clear_completed_report_requires_confirmation(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_COMPLETED,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="清空当前草稿", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "ask_confirm_clear_current_report"
    assert result.today_work == ["处理合同审核"]
    assert result.tomorrow_plan == ["继续跟进审批"]
    assert result.section_status[report_service.PENDING_ACTION_KEY] == report_service.PENDING_ACTION_CONFIRM_CLEAR_CURRENT_REPORT
    assert "当前复盘已经提交" in result.message
    assert "回复“清空”确认" in result.message


@pytest.mark.asyncio
async def test_pending_action_confirm_clear_runs_before_courtesy(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": True,
                report_service.PENDING_ACTION_KEY: report_service.PENDING_ACTION_CONFIRM_CLEAR_CURRENT_REPORT,
            },
            status=STATUS_COMPLETED,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="确定", source="test")

    assert result.report_saved is True
    assert result.reply_kind == "clear_current_report"
    assert result.status == STATUS_COLLECTING
    assert result.today_work == []
    assert result.problems == []
    assert result.tomorrow_plan == []
    assert report_service.PENDING_ACTION_KEY not in result.section_status
    assert "不客气" not in result.message
    assert "已清空当前复盘草稿" in result.message


@pytest.mark.asyncio
async def test_pending_action_cancel_clear_keeps_report(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": True,
                report_service.PENDING_ACTION_KEY: report_service.PENDING_ACTION_CONFIRM_CLEAR_CURRENT_REPORT,
            },
            status=STATUS_COMPLETED,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="取消", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "cancel_clear_current_report"
    assert result.status == STATUS_COMPLETED
    assert result.today_work == ["处理合同审核"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["继续跟进审批"]
    assert report_service.PENDING_ACTION_KEY not in result.section_status
    assert "已保留" in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_text", ["清空", "先帮我清空", "这版不要了", "从头开始"])
async def test_clear_instruction_never_enters_report_fields(monkeypatch, clear_text):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_COLLECTING,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input=clear_text, source="test")

    assert result.today_work == []
    assert result.problems == []
    assert result.tomorrow_plan == []
    assert all(clear_text not in item for item in result.today_work + result.problems + result.tomorrow_plan)


@pytest.mark.asyncio
async def test_llm_intent_can_clear_current_report_without_keyword(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor(
        [],
        intent_outputs=[
            DailyInputIntentDecision(
                intent="clear_current_report",
                confidence=0.9,
                target_field="all",
                should_discard_previous=True,
                should_update_report=True,
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)
    result = await service.submit_text(FakeSession(), user=user, raw_input="先不要保留目前整理的内容，后面重新填", source="test")

    assert result.report_saved is True
    assert result.reply_kind == "clear_current_report"
    assert result.today_work == []
    assert result.problems == []
    assert result.tomorrow_plan == []


@pytest.mark.asyncio
async def test_short_reply_means_no_problem_only_when_asking_problems(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            section_status={"today_work": True, "problems": False, "tomorrow_plan": False},
            status=STATUS_COLLECTING,
            completeness_score=0.34,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="正常", source="test")

    assert result.report_saved is True
    assert result.problems == ["暂无明显问题"]
    assert result.missing_sections == ["tomorrow_plan"]


@pytest.mark.asyncio
async def test_short_no_problem_variants_are_fast_path_when_asking_problems(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            section_status={"today_work": True, "problems": False, "tomorrow_plan": False},
            status=STATUS_COLLECTING,
            completeness_score=0.34,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="没有没有", source="test")

    assert result.report_saved is True
    assert result.problems == ["暂无明显问题"]
    assert result.timings["llm_extract_seconds"] == 0.0


@pytest.mark.asyncio
async def test_short_reply_is_not_today_work_when_asking_today_work(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return None

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="还行", source="test")

    assert result.report_saved is False
    assert result.today_work == []
    assert result.missing_sections == ["today_work", "problems", "tomorrow_plan"]


@pytest.mark.asyncio
async def test_short_reply_is_not_tomorrow_plan_when_asking_tomorrow_plan(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": False,
                "problems_acknowledged_empty": True,
            },
            status=STATUS_COLLECTING,
            completeness_score=0.67,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="收到", source="test")

    assert result.report_saved is False
    assert result.tomorrow_plan == []
    assert result.missing_sections == ["tomorrow_plan"]


@pytest.mark.asyncio
async def test_rewrite_after_test_content_replaces_old_draft(monkeypatch):
    stored = {"report": None}
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(
            id=stored["report"].id if stored["report"] else uuid4(),
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
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor(
        [
            StructuredDailyReport(today_work=["吃了手抓饼"], completeness=0.34),
            StructuredDailyReport(problems=["蛋没给我加全"], tomorrow_plan=["明天去苏州吃更好吃的"], completeness=0.66),
            StructuredDailyReport(
                today_work=["跟你实话实说吧，今天审了三个合同"],
                problems=[],
                tomorrow_plan=["明天可能会去南京开个庭"],
                completeness=0.67,
            ),
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    await service.submit_text(FakeSession(), user=user, raw_input="吃了手抓饼", source="test")
    await service.submit_text(FakeSession(), user=user, raw_input="蛋没给我加全，明天去苏州吃更好吃的", source="test")
    result = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="算了算了，跟你实话实说吧，今天审了三个合同，然后没啥问题，明天可能会去南京开个庭",
        source="test",
    )

    assert result.status == STATUS_PENDING_CONFIRMATION
    assert result.today_work == ["今天审了三个合同"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["明天可能会去南京开个庭"]
    assert "手抓饼" not in result.message
    assert "蛋没给我加全" not in result.message
    assert "审了三个合同" in result.message


@pytest.mark.asyncio
async def test_llm_intent_can_replace_complex_non_keyword_rewrite(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["吃了手抓饼"],
            problems=["蛋没给我加全"],
            tomorrow_plan=["明天去苏州吃手抓饼"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor(
        [StructuredDailyReport(today_work=["今天审了三个合同"], problems=[], tomorrow_plan=["明天去南京开庭"], completeness=0.67)],
        intent_outputs=[
            DailyInputIntentDecision(
                intent="replace_current_report",
                confidence=0.88,
                target_field="all",
                should_discard_previous=True,
                should_update_report=True,
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)
    result = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="刚才开玩笑的，今天其实审了三个合同，没啥问题，明天去南京开庭",
        source="test",
    )

    assert result.status == STATUS_PENDING_CONFIRMATION
    assert result.today_work == ["今天审了三个合同"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["明天去南京开庭"]
    assert "手抓饼" not in result.message


@pytest.mark.asyncio
async def test_low_confidence_completed_rewrite_asks_before_overwrite(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_COMPLETED,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor(
        [],
        intent_outputs=[
            DailyInputIntentDecision(
                intent="replace_current_report",
                confidence=0.7,
                target_field="all",
                should_discard_previous=True,
                should_update_report=True,
                clarification_question="要用这条覆盖已提交的复盘吗？",
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)
    result = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="今天其实换成审了三个合同",
        source="test",
    )

    assert result.report_saved is False
    assert result.reply_kind == "uncertain_high_risk"
    assert result.today_work == ["处理合同审核"]
    assert "覆盖" in result.message


@pytest.mark.asyncio
async def test_high_confidence_completed_rewrite_still_does_not_overwrite(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["处理合同审核"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续跟进审批"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_COMPLETED,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor(
        [],
        intent_outputs=[
            DailyInputIntentDecision(
                intent="replace_current_report",
                confidence=0.98,
                target_field="all",
                should_discard_previous=True,
                should_update_report=True,
                clarification_question="要用这条覆盖已提交的复盘吗？",
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)
    result = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="今天其实换成审了三个合同",
        source="test",
    )

    assert result.report_saved is False
    assert result.reply_kind == "uncertain_high_risk"
    assert result.today_work == ["处理合同审核"]
    assert result.tomorrow_plan == ["继续跟进审批"]


@pytest.mark.asyncio
async def test_explicit_append_adds_to_existing_work(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=["审了三个合同"],
            section_status={"today_work": True, "problems": False, "tomorrow_plan": False},
            status=STATUS_COLLECTING,
            completeness_score=0.34,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor([StructuredDailyReport(today_work=["参加了一个项目会议"], completeness=0.34)])
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)
    result = await service.submit_text(FakeSession(), user=user, raw_input="对了，还参加了一个项目会议", source="test")

    assert result.today_work == ["审了三个合同", "参加了一个项目会议"]


@pytest.mark.asyncio
async def test_colloquial_fillers_are_not_written_to_fields(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return None

    async def fake_upsert_daily_report(session, **kwargs):
        return _make_report(
            report_date=kwargs["report_date"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
        )

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor(
        [
            StructuredDailyReport(
                today_work=["哎，跟你实话实说吧，今天审了三个合同", "你说人活着是为了什么呢"],
                problems=[],
                tomorrow_plan=[],
                completeness=0.34,
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)
    result = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="哎，跟你实话实说吧，今天审了三个合同。你说人活着是为了什么呢",
        source="test",
    )

    assert result.today_work == ["今天审了三个合同"]
    assert "人活着" not in result.message


@pytest.mark.asyncio
async def test_modify_tomorrow_reply_uses_update_wording_and_missing_followup(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        "report": _make_report(
            today_work=[],
            problems=[],
            tomorrow_plan=["明天去上海开庭"],
            section_status={"today_work": False, "problems": False, "tomorrow_plan": True},
            status=STATUS_COLLECTING,
            completeness_score=0.33,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        stored["report"] = _make_report(**{**stored["report"].__dict__, **kwargs})
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor([]))
    result = await service.submit_text(FakeSession(), user=user, raw_input="帮我改下，明天去广州开庭", source="test")

    assert result.tomorrow_plan == ["明天去广州开庭"]
    assert result.timings["llm_extract_seconds"] == 0.0
    assert "已把明日计划更新为：明天去广州开庭。" in result.message
    assert "现在还差两项：今天主要做了什么、有没有遇到问题或风险？" in result.message
    assert "补充一下" not in result.message


@pytest.mark.asyncio
async def test_simple_extract_preserves_quantity_detail_from_raw_input(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return None

    async def fake_upsert_daily_report(session, **kwargs):
        return _make_report(
            report_date=kwargs["report_date"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
        )

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 14))

    extractor = FakeExtractor([StructuredDailyReport(today_work=["完成拟诉评估"], completeness=0.34)])
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)
    result = await service.submit_text(FakeSession(), user=user, raw_input="完成了几个拟诉评估", source="test")

    assert result.today_work == ["完成了几个拟诉评估"]
    assert "完成拟诉评估" not in result.message


@pytest.mark.asyncio
async def test_lujian_backfill_dialogue_before_9_keeps_yesterday_draft_and_moves_items(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {}

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        report_date = kwargs["report_date"]
        existing = stored.get(report_date)
        fragments = list(getattr(existing, "input_fragments", []) if existing else [])
        fragments.append({"raw_input": kwargs["raw_input"], "structured": kwargs["llm_payload"]})
        raw_input = kwargs["raw_input"] if existing is None else "\n".join([getattr(existing, "raw_input", ""), kwargs["raw_input"]]).strip()
        report = _make_report(
            id=getattr(existing, "id", uuid4()),
            report_date=report_date,
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            emotion=kwargs["emotion"],
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
            raw_input=raw_input,
            input_fragments=fragments,
        )
        stored[report_date] = report
        return report

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 8, 31, 56))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="report_update",
                message_kind="report_content",
                operation="set_fields",
                target_field="today_work",
                target_report_date="2026-06-17",
                confidence=0.95,
                should_write=True,
                field_updates=[
                    {
                        "field": "today_work",
                        "mode": "replace",
                        "items": [
                            "完成线上流程审批92个",
                            "完成协议用印3个",
                            "完成协议归档4个",
                            "完成法务审批关键节点培训PPT制作及困难点分析",
                        ],
                    }
                ],
            ),
            DraftDecision(
                decision_type="report_update",
                message_kind="report_content",
                operation="append",
                target_field="today_work",
                confidence=0.9,
                should_write=True,
                field_updates=[
                    {
                        "field": "today_work",
                        "mode": "append",
                        "items": ["完成合同移交整理"],
                    }
                ],
            ),
            DraftDecision(
                decision_type="draft_edit",
                message_kind="draft_edit_instruction",
                operation="move_item",
                confidence=0.95,
                should_write=True,
                move_items=[
                    {
                        "source_field": "today_work",
                        "destination_field": "tomorrow_plan",
                        "item_refs": [5, 6],
                        "source_item_text": "完成日常工作 完成合同移交整理",
                    }
                ],
            ),
            DraftDecision(
                decision_type="clarification",
                message_kind="ambiguous",
                operation="clarify",
                confidence=0.9,
                should_write=False,
                reply_to_user="should not win over backend date acknowledgement",
            ),
            DraftDecision(
                decision_type="clarification",
                message_kind="ambiguous",
                operation="clarify",
                confidence=0.9,
                should_write=False,
                reply_to_user="should not win over backend satisfied move acknowledgement",
            ),
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    first = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="昨天完成线上流程审批92个协议用印是三个，协议的归档是四个。然后还对法务审批关键节点的培训，PPT的制作困难点进行了分析",
        source="test",
    )
    second = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="今天主要工作都是日常工作，然后还有一些合同移交的整理",
        source="test",
    )
    ambiguous_problem = await service.submit_text(FakeSession(), user=user, raw_input="好像有一个", source="test")
    corrected = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="修正第二第三项的数量分别为十三个和十四个。第五第六个是今天的计划，一到四都是昨天的完成",
        source="test",
    )
    date_note = await service.submit_text(FakeSession(), user=user, raw_input="以上内容都是补交6月17号的不是今天的", source="test")
    final = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="一到四项是6月17号完成事项。五到六项是6月18号待完成事项",
        source="test",
    )

    assert first.report_date == date(2026, 6, 17)
    assert second.report_date == date(2026, 6, 17)
    assert second.today_work == [
        "完成线上流程审批92个",
        "完成协议用印3个",
        "完成协议归档4个",
        "完成法务审批关键节点培训PPT制作及困难点分析",
    ]
    assert second.tomorrow_plan == ["完成日常工作", "完成合同移交整理"]
    assert ambiguous_problem.report_saved is False
    assert ambiguous_problem.reply_kind == "problem_clarification"
    assert "具体是什么" in ambiguous_problem.message
    assert corrected.report_date == date(2026, 6, 17)
    assert corrected.report_saved is True
    assert corrected.today_work == [
        "完成线上流程审批92个",
        "完成协议用印13个",
        "完成协议归档14个",
        "完成法务审批关键节点培训PPT制作及困难点分析",
    ]
    assert corrected.tomorrow_plan == ["完成日常工作", "完成合同移交整理"]
    assert date_note.report_saved is False
    assert "当前正在填写的是 2026-06-17 的日报" in date_note.message
    assert "从哪一栏移动到哪一栏" not in date_note.message
    assert final.report_date == date(2026, 6, 17)
    assert final.today_work == [
        "完成线上流程审批92个",
        "完成协议用印13个",
        "完成协议归档14个",
        "完成法务审批关键节点培训PPT制作及困难点分析",
    ]
    assert final.problems == []
    assert final.tomorrow_plan == ["完成日常工作", "完成合同移交整理"]
    assert final.missing_sections == ["problems"]
    assert "暂无明显问题" not in "\n".join(stored[date(2026, 6, 17)].problems)

    final_item_ids = stored[date(2026, 6, 17)].section_status[report_service.DRAFT_ITEM_IDS_KEY]
    assert len(final_item_ids["today_work"]) == 4
    assert len(final_item_ids["tomorrow_plan"]) == 2
    assert len(set(final_item_ids["today_work"] + final_item_ids["tomorrow_plan"])) == 6
    assert len(extractor.draft_outputs) == 0


@pytest.mark.asyncio
async def test_backfill_actual_day_phrases_route_to_tomorrow_plan(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        date(2026, 6, 17): _make_report(
            report_date=date(2026, 6, 17),
            today_work=["完成昨天事项"],
            problems=[],
            tomorrow_plan=[],
            section_status={"today_work": True, "problems": False, "tomorrow_plan": False},
            status=STATUS_COLLECTING,
            completeness_score=0.34,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        report_date = kwargs["report_date"]
        existing = stored.get(report_date)
        report = _make_report(
            id=getattr(existing, "id", uuid4()),
            report_date=report_date,
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            emotion=kwargs["emotion"],
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
        )
        stored[report_date] = report
        return report

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 8, 31, 0))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="report_update",
                message_kind="report_content",
                operation="append",
                target_field="today_work",
                confidence=0.9,
                should_write=True,
                field_updates=[{"field": "today_work", "mode": "append", "items": [f"完成{index}"]}],
            )
            for index in range(1, 7)
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    actual_day_inputs = [
        "今天主要工作是日常工作",
        "今日主要工作是合同移交",
        "今儿主要工作是材料整理",
        "today mainly review contracts",
        "6月18号主要工作是审批跟进",
    ]
    for raw_input in actual_day_inputs:
        result = await service.submit_text(FakeSession(), user=user, raw_input=raw_input, source="test")
        assert result.report_date == date(2026, 6, 17)

    same_report_day = await service.submit_text(FakeSession(), user=user, raw_input="6月17号主要工作是归档", source="test")

    report = stored[date(2026, 6, 17)]
    assert report.today_work == ["完成昨天事项", "完成6"]
    assert report.tomorrow_plan == ["完成日常工作", "完成1", "完成2", "完成3", "完成4", "完成5"]
    assert same_report_day.today_work == ["完成昨天事项", "完成6"]
    assert same_report_day.tomorrow_plan == ["完成日常工作", "完成1", "完成2", "完成3", "完成4", "完成5"]


@pytest.mark.asyncio
async def test_backfill_ordinal_move_to_tomorrow_plan_uses_backend_shadow_when_llm_disagrees(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        date(2026, 6, 17): _make_report(
            report_date=date(2026, 6, 17),
            today_work=["事项一", "事项二", "事项三", "事项四"],
            problems=[],
            tomorrow_plan=[],
            section_status={"today_work": True, "problems": False, "tomorrow_plan": False},
            status=STATUS_COLLECTING,
            completeness_score=0.34,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        report_date = kwargs["report_date"]
        existing = stored.get(report_date)
        report = _make_report(
            id=getattr(existing, "id", uuid4()),
            report_date=report_date,
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            emotion=kwargs["emotion"],
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
        )
        stored[report_date] = report
        return report

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 8, 31, 0))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="draft_edit",
                message_kind="draft_edit_instruction",
                operation="move_item",
                confidence=0.95,
                should_write=True,
                move_items=[
                    {
                        "source_field": "tomorrow_plan",
                        "destination_field": "tomorrow_plan",
                        "item_refs": [3, 4],
                        "source_item_text": "错误条目",
                    }
                ],
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    result = await service.submit_text(FakeSession(), user=user, raw_input="今日第三第四项目移到明日里", source="test")

    assert result.report_date == date(2026, 6, 17)
    assert result.report_saved is True
    assert result.today_work == ["事项一", "事项二"]
    assert result.tomorrow_plan == ["事项三", "事项四"]
    assert len(extractor.draft_outputs) == 0


@pytest.mark.asyncio
async def test_current_day_enumerated_work_before_tomorrow_anchor_stays_in_today_work(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {}

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        report_date = kwargs["report_date"]
        existing = stored.get(report_date)
        report = _make_report(
            id=getattr(existing, "id", uuid4()),
            report_date=report_date,
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            emotion=kwargs["emotion"],
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
        )
        stored[report_date] = report
        return report

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 18, 20, 0))

    raw_input = (
        "今天主要处理三件事：（一）完成印章保管承诺书修订并发业务确认；"
        "（二）核对南京项目结算资料，发现审计报告还没提供；"
        "（三）跟进苏州项目开庭准备。明天继续催审计报告。"
    )
    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="report_update",
                message_kind="long_report_content",
                operation="set_fields",
                target_field="all",
                confidence=0.95,
                should_write=True,
                field_updates=[
                    {
                        "field": "today_work",
                        "mode": "replace",
                        "items": [
                            "完成印章保管承诺书修订并发业务确认",
                            "核对南京项目结算资料，发现审计报告还没提供",
                        ],
                    },
                    {
                        "field": "problems",
                        "mode": "replace",
                        "items": ["南京项目审计报告尚未提供"],
                    },
                    {
                        "field": "tomorrow_plan",
                        "mode": "replace",
                        "items": ["跟进苏州项目开庭准备", "明日继续催收审计报告"],
                    },
                ],
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    result = await service.submit_text(FakeSession(), user=user, raw_input=raw_input, source="test")

    assert result.report_date == date(2026, 6, 18)
    assert result.report_saved is True
    assert result.today_work == [
        "完成印章保管承诺书修订并发业务确认",
        "核对南京项目结算资料，发现审计报告还没提供",
        "跟进苏州项目开庭准备",
    ]
    assert result.problems == ["南京项目审计报告尚未提供"]
    assert result.tomorrow_plan == ["明日继续催收审计报告"]


@pytest.mark.asyncio
async def test_daily_briefing_feedback_is_not_written_to_personal_report(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return None

    async def fake_upsert_daily_report(session, **kwargs):
        raise AssertionError("daily briefing feedback should not write a personal report")

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 10, 0, 0))

    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), FakeDraftExtractor([]))

    result = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="我想改一下这个晨报总览，太复杂了，负责人关注只保留风险+问题+明日计划",
        source="test",
    )

    assert result.report_saved is False
    assert result.reply_kind == "daily_briefing_feedback"
    assert "不会写入你的个人日报" in result.message


@pytest.mark.asyncio
async def test_mamin_single_item_correction_does_not_duplicate_full_field_tail(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {}

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        report_date = kwargs["report_date"]
        existing = stored.get(report_date)
        report = _make_report(
            id=getattr(existing, "id", uuid4()),
            report_date=report_date,
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            emotion=kwargs["emotion"],
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
        )
        stored[report_date] = report
        return report

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 17))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 22, 4, 0))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="report_update",
                message_kind="report_content",
                operation="set_fields",
                target_field="today_work",
                confidence=0.95,
                should_write=True,
                field_updates=[
                    {
                        "field": "today_work",
                        "mode": "replace",
                        "items": [
                            "审核用眼流程",
                            "协议登记",
                            "对26年以来未归档的协议进行统计分析，并用workbody制作表格，准备由总裁办进行催收",
                        ],
                    }
                ],
            ),
            DraftDecision(
                decision_type="report_update",
                message_kind="report_content",
                operation="set_fields",
                target_field="none",
                confidence=0.95,
                should_write=True,
                field_updates=[
                    {"field": "problems", "mode": "replace", "items": ["暂无明显问题"]},
                    {"field": "tomorrow_plan", "mode": "replace", "items": ["继续今日工作"]},
                ],
            ),
            DraftDecision(
                decision_type="draft_edit",
                message_kind="draft_edit_instruction",
                operation="rewrite_item",
                target_field="today_work",
                confidence=0.95,
                should_write=True,
                field_updates=[
                    {
                        "field": "today_work",
                        "mode": "replace",
                        "item_refs": [1],
                        "items": [
                            "总审核用印流程",
                            "协议登记",
                            "对26年以来未归档的协议进行统计分析，并用workbody制作表格，准备由总裁办进行催收",
                        ],
                    }
                ],
            ),
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="今天最主要的工作是：一就是审核用眼流程；二就是协议登记；第三个就是把26年以来未归档的协议进行统计分析，然后用workbody把它做成表格准备由总裁办来进行催收",
        source="test",
    )
    await service.submit_text(FakeSession(), user=user, raw_input="没有什么问题和风险，明天还要继续这些工作", source="test")
    corrected = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="第一条是总审核用印流程不是用眼不是眼睛的眼用印印章的印",
        source="test",
    )

    assert corrected.today_work[0] == "总审核用印流程"
    assert corrected.today_work[1] == "协议登记"
    assert corrected.today_work.count("协议登记") == 1
    assert len(corrected.today_work) == 3
    assert corrected.problems == ["暂无明显问题"]
    assert corrected.tomorrow_plan == ["继续今日工作"]


@pytest.mark.asyncio
async def test_duplicate_pair_correction_removes_later_duplicates_without_llm(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        date(2026, 6, 17): _make_report(
            report_date=date(2026, 6, 17),
            today_work=[
                "总审核用印流程",
                "协议登记",
                "对26年以来未归档的协议进行统计分析，并用workbody制作表格，准备由总裁办进行催收",
                "协议登记",
                "对26年以来未归档的协议进行统计分析，并用workbody制作表格，准备由总裁办进行催收",
            ],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续今日工作"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        report_date = kwargs["report_date"]
        existing = stored.get(report_date)
        report = _make_report(
            id=getattr(existing, "id", uuid4()),
            report_date=report_date,
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            emotion=kwargs["emotion"],
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
        )
        stored[report_date] = report
        return report

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 17))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 22, 15, 39))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="clarification",
                message_kind="ambiguous",
                operation="clarify",
                confidence=0.9,
                should_write=False,
                reply_to_user="should not be called",
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    result = await service.submit_text(FakeSession(), user=user, raw_input="这条信息里的2和4、3和5重复了", source="test")

    assert result.report_saved is True
    assert result.reply_kind == "pending_confirmation"
    assert result.today_work == [
        "总审核用印流程",
        "协议登记",
        "对26年以来未归档的协议进行统计分析，并用workbody制作表格，准备由总裁办进行催收",
    ]
    assert len(extractor.draft_outputs) == 0


@pytest.mark.asyncio
async def test_duplicate_pair_correction_without_verified_duplicates_keeps_draft_when_llm_disagrees(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        date(2026, 6, 17): _make_report(
            report_date=date(2026, 6, 17),
            today_work=["总审核用印流程", "协议登记", "统计分析"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续今日工作"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        raise AssertionError("unverified duplicate correction should not write")

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 17))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 22, 15, 39))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="draft_edit",
                message_kind="draft_edit_instruction",
                operation="delete_item",
                confidence=0.95,
                should_write=True,
                delete_items=[{"target_field": "today_work", "item_refs": [2], "target_item_text": "协议登记"}],
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    result = await service.submit_text(FakeSession(), user=user, raw_input="这条信息里的2和4、3和5重复了", source="test")

    assert result.report_saved is False
    assert result.today_work == ["总审核用印流程", "协议登记", "统计分析"]
    assert "没有发现你说的这些编号是重复项" in result.message
    assert len(extractor.draft_outputs) == 0


@pytest.mark.asyncio
async def test_rejected_draft_edit_marks_unresolved_and_blocks_service_auto_submit(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    report_date = date(2026, 6, 17)
    stored = {
        report_date: _make_report(
            report_date=report_date,
            today_work=["总审核用印流程"],
            problems=["暂无明显问题"],
            tomorrow_plan=["继续今日工作"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
            pending_confirmation_at=datetime(2026, 6, 17, 22, 0, 0),
            auto_submit_at=datetime(2026, 6, 17, 22, 30, 0),
        )
    }

    async def fake_get_report(session, user_id, requested_report_date):
        return stored.get(requested_report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        raise AssertionError("failed edit should not rewrite the report")

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: report_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 22, 15, 0))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="draft_edit",
                message_kind="draft_edit_instruction",
                operation="delete_item",
                target_field="today_work",
                confidence=0.95,
                should_write=True,
                delete_items=[
                    {
                        "target_field": "today_work",
                        "item_refs": [9],
                        "target_item_text": "不存在的条目",
                    }
                ],
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    result = await service.submit_text(FakeSession(), user=user, raw_input="第九条重复了，删掉", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "draft_decision_rejected"
    assert report_service.UNRESOLVED_DRAFT_EDIT_KEY in stored[report_date].section_status
    assert report_service._should_auto_submit(stored[report_date], datetime(2026, 6, 17, 23, 0, 0)) is False


@pytest.mark.asyncio
async def test_due_pending_confirmation_user_message_is_processed_before_auto_submit(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    report_date = date(2026, 6, 17)
    stored = {
        report_date: _make_report(
            report_date=report_date,
            today_work=["old item"],
            problems=["none"],
            tomorrow_plan=["next item"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
            pending_confirmation_at=datetime(2026, 6, 17, 21, 30, 0),
            auto_submit_at=datetime(2026, 6, 17, 22, 0, 0),
        )
    }

    async def fake_get_report(session, user_id, requested_report_date):
        return stored.get(requested_report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        existing = stored.get(kwargs["report_date"])
        report = _make_report(
            id=getattr(existing, "id", uuid4()),
            report_date=kwargs["report_date"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
            section_status=kwargs["section_status"],
            status=kwargs["status"],
            completeness_score=kwargs["completeness_score"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            emotion=kwargs["emotion"],
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
        )
        stored[kwargs["report_date"]] = report
        return report

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: report_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 22, 15, 0))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="report_update",
                message_kind="report_content",
                operation="append",
                target_field="today_work",
                confidence=0.9,
                should_write=True,
                field_updates=[{"field": "today_work", "mode": "append", "items": ["new item"]}],
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    result = await service.submit_text(FakeSession(), user=user, raw_input="append a new item", source="test")

    assert result.report_saved is True
    assert result.status == STATUS_PENDING_CONFIRMATION
    assert result.confirmation_type == CONFIRMATION_NONE
    assert result.confirmed_by_user is False
    assert result.today_work == ["old item", "new item"]
    assert result.timings["pre_message_auto_submit_due"] is True


@pytest.mark.asyncio
async def test_confirm_does_not_bypass_unresolved_draft_edit(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    report_date = date(2026, 6, 17)
    stored = {
        report_date: _make_report(
            report_date=report_date,
            today_work=["old item"],
            problems=["none"],
            tomorrow_plan=["next item"],
            section_status={
                "today_work": True,
                "problems": True,
                "tomorrow_plan": True,
                report_service.UNRESOLVED_DRAFT_EDIT_KEY: {"error": "failed edit"},
            },
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, requested_report_date):
        return stored.get(requested_report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        raise AssertionError("unresolved edit must block confirmation writes")

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: report_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 22, 15, 0))

    extractor = FakeDraftExtractor([])
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    result = await service.submit_text(FakeSession(), user=user, raw_input="ok", source="test")

    assert result.report_saved is False
    assert result.reply_kind == "confirm_blocked_by_unresolved_edit"
    assert result.status == STATUS_PENDING_CONFIRMATION


@pytest.mark.asyncio
async def test_draft_edit_clarification_sets_unresolved_and_blocks_confirm(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    report_date = date(2026, 6, 17)
    stored = {
        report_date: _make_report(
            report_date=report_date,
            today_work=["old item"],
            problems=["none"],
            tomorrow_plan=["next item"],
            section_status={"today_work": True, "problems": True, "tomorrow_plan": True},
            status=STATUS_PENDING_CONFIRMATION,
            completeness_score=1.0,
        )
    }

    async def fake_get_report(session, user_id, requested_report_date):
        return stored.get(requested_report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        raise AssertionError("clarified edit must block confirmation writes")

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: report_date)
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 17, 22, 15, 0))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="clarification",
                message_kind="draft_edit_instruction",
                operation="delete_item",
                target_field="none",
                confidence=0.9,
                should_write=False,
                needs_clarification=True,
                reply_to_user="current draft has no item 9",
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    bad_edit = await service.submit_text(FakeSession(), user=user, raw_input="delete item 9", source="test")
    confirm = await service.submit_text(FakeSession(), user=user, raw_input="ok", source="test")

    assert bad_edit.report_saved is False
    assert bad_edit.reply_kind == "draft_decision_clarification"
    assert report_service.UNRESOLVED_DRAFT_EDIT_KEY in stored[report_date].section_status
    assert confirm.report_saved is False
    assert confirm.reply_kind == "confirm_blocked_by_unresolved_edit"
    assert confirm.status == STATUS_PENDING_CONFIRMATION


@pytest.mark.asyncio
async def test_backfill_entry_prompt_uses_resolved_report_date_without_llm(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")

    async def fake_get_report(session, user_id, report_date):
        return None

    async def fake_upsert_daily_report(session, **kwargs):
        raise AssertionError("date entry prompt should not write a report")

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 8, 31, 0))

    extractor = FakeDraftExtractor(
        [
            DraftDecision(
                decision_type="clarification",
                message_kind="ambiguous",
                operation="clarify",
                confidence=0.9,
                should_write=False,
                needs_clarification=True,
                reply_to_user="好的，正在补写2026-06-16的日报。请告诉我您昨天的主要工作内容。",
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    result = await service.submit_text(FakeSession(), user=user, raw_input="我在写昨天的日报", source="test")

    assert result.report_date == date(2026, 6, 17)
    assert result.report_saved is False
    assert result.reply_kind == "report_date_clarification"
    assert "当前填报日期：2026-06-17" in result.message
    assert "正在填写 2026-06-17 的日报" in result.message
    assert "2026-06-16" not in result.message
    assert len(extractor.draft_outputs) == 1


@pytest.mark.asyncio
async def test_previous_day_backfill_and_edit_are_blocked_after_9(monkeypatch):
    user = SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai")
    stored = {
        date(2026, 6, 17): _make_report(
            report_date=date(2026, 6, 17),
            today_work=["完成协议用印3个"],
            section_status={"today_work": True, "problems": False, "tomorrow_plan": False},
            status=STATUS_COLLECTING,
            completeness_score=0.34,
        )
    }

    async def fake_get_report(session, user_id, report_date):
        return stored.get(report_date)

    async def fake_upsert_daily_report(session, **kwargs):
        raise AssertionError("previous-day cutoff should reject before writing")

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 18))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 18, 9, 1, 0))

    extractor = FakeDraftExtractor([])
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai", llm_draft_decision_enabled=True), extractor)

    explicit_backfill = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="补交昨天的日报，昨天完成合同审核",
        source="test",
    )
    manual_report_date_backfill = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="manual explicit previous day backfill",
        source="test",
        report_date=date(2026, 6, 17),
    )
    implicit_edit = await service.submit_text(
        FakeSession(),
        user=user,
        raw_input="修正第二项数量为十三个",
        source="test",
    )
    undo = await service.submit_text(FakeSession(), user=user, raw_input="撤回上一步操作", source="test")

    assert explicit_backfill.report_saved is False
    assert manual_report_date_backfill.report_saved is False
    assert implicit_edit.report_saved is False
    assert undo.report_saved is False
    assert explicit_backfill.reply_kind == "previous_report_cutoff"
    assert manual_report_date_backfill.reply_kind == "previous_report_cutoff"
    assert implicit_edit.reply_kind == "previous_report_cutoff"
    assert undo.reply_kind == "previous_report_cutoff"
    assert "09:00 前" in explicit_backfill.message
    assert extractor.draft_outputs == []
