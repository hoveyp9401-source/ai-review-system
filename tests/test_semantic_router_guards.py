from datetime import date, datetime
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
        self.extract_calls = 0
        self.intent_calls = 0
        self.raw_inputs = []
        self.client = SimpleNamespace(model="fake")

    async def extract(self, raw_input):
        self.extract_calls += 1
        self.raw_inputs.append(raw_input)
        if self.outputs:
            return self.outputs.pop(0)
        return StructuredDailyReport()

    async def decide_intent(self, *, raw_input, context):
        self.intent_calls += 1
        if self.intent_outputs:
            return self.intent_outputs.pop(0)
        return DailyInputIntentDecision(
            message_kind="report_content",
            intent="continue_collecting",
            confidence=0.8,
        )


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


def _install_store(monkeypatch, initial=None, *, fail_on_upsert=False):
    stored = {"report": initial}

    async def fake_get_report(session, user_id, report_date):
        return stored["report"]

    async def fake_upsert_daily_report(session, **kwargs):
        if fail_on_upsert:
            raise AssertionError("upsert should not be called")
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
            submitted_at=kwargs["received_at"] if kwargs["status"] == "completed" else None,
        )
        return stored["report"]

    monkeypatch.setattr(report_service, "get_report", fake_get_report)
    monkeypatch.setattr(report_service, "upsert_daily_report", fake_upsert_daily_report)
    monkeypatch.setattr(report_service, "today_in_timezone", lambda timezone: date(2026, 6, 16))
    monkeypatch.setattr(report_service, "now_in_timezone", lambda timezone: datetime(2026, 6, 16, 10, 0))
    return stored


def _decision(**overrides):
    values = {
        "message_kind": "report_content",
        "intent": "continue_collecting",
        "operation": "set_fields",
        "target_field": "none",
        "relation_to_existing": "none",
        "confidence": 0.8,
    }
    values.update(overrides)
    return DailyInputIntentDecision(**values)


@pytest.mark.asyncio
async def test_collecting_missing_problems_rejects_ambiguous_slot_input(monkeypatch):
    existing = _make_report(today_work=["今天吃了手抓饼"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing, fail_on_upsert=True)
    extractor = FakeExtractor(outputs=[StructuredDailyReport()], intent_outputs=[_decision()])
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="晴空", source="test")

    assert result.report_saved is False
    assert result.problems == []
    assert result.reply_kind == "slot_semantic_rejected"
    assert "不太像问题/风险" in result.message
    assert result.timings["semantic_router_used"] is True
    assert result.timings["executor_decision"] == "reject"
    assert result.timings["reject_reason"] == "slot_semantic_rejected"


@pytest.mark.asyncio
async def test_collecting_missing_problems_rejects_test_noise(monkeypatch):
    existing = _make_report(today_work=["处理恒大事务"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing, fail_on_upsert=True)
    extractor = FakeExtractor(outputs=[StructuredDailyReport()], intent_outputs=[_decision()])
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="测试", source="test")

    assert result.report_saved is False
    assert result.problems == []
    assert result.reply_kind == "slot_semantic_rejected"


@pytest.mark.asyncio
async def test_collecting_missing_problems_allows_clear_no_problem_reply(monkeypatch):
    existing = _make_report(today_work=["处理合同审核"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor()
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="没问题", source="test")

    assert result.report_saved is True
    assert result.problems == ["暂无明显问题"]
    assert extractor.intent_calls == 0


@pytest.mark.asyncio
async def test_collecting_missing_tomorrow_rejects_ambiguous_slot_input(monkeypatch):
    existing = _make_report(today_work=["处理合同审核"], problems=["暂无明显问题"], tomorrow_plan=[])
    _install_store(monkeypatch, existing, fail_on_upsert=True)
    extractor = FakeExtractor(outputs=[StructuredDailyReport()], intent_outputs=[_decision()])
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="晴空", source="test")

    assert result.report_saved is False
    assert result.tomorrow_plan == []
    assert result.reply_kind == "slot_semantic_rejected"
    assert "不太像明日计划" in result.message


@pytest.mark.asyncio
async def test_collecting_missing_tomorrow_allows_router_targeted_plan(monkeypatch):
    existing = _make_report(today_work=["处理合同审核"], problems=["暂无明显问题"], tomorrow_plan=[])
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        outputs=[StructuredDailyReport(tomorrow_plan=["明天开庭"], completeness=0.33)],
        intent_outputs=[_decision(target_field="tomorrow_plan")],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="明天开庭", source="test")

    assert result.report_saved is True
    assert result.tomorrow_plan == ["明天开庭"]


@pytest.mark.asyncio
async def test_new_today_item_does_not_fill_missing_problems(monkeypatch):
    existing = _make_report(today_work=["今天吃了手抓饼"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        outputs=[StructuredDailyReport(today_work=["处理了恒大事务"], completeness=0.34)],
        intent_outputs=[_decision(intent="append_to_existing", operation="append", target_field="today_work", relation_to_existing="new_item", should_append=True)],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="处理了恒大事务", source="test")

    assert result.report_saved is True
    assert result.today_work == ["今天吃了手抓饼", "处理了恒大事务"]
    assert result.problems == []


@pytest.mark.asyncio
async def test_complete_report_after_existing_draft_appends_new_today_work(monkeypatch):
    existing = _make_report(today_work=["跟进恒大项目保全资料补充情况"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        outputs=[
            StructuredDailyReport(
                today_work=["审了三个合同"],
                problems=["暂无明显问题"],
                tomorrow_plan=["明天开庭"],
                completeness=1.0,
            )
        ],
        intent_outputs=[
            _decision(
                intent="continue_collecting",
                operation="set_fields",
                target_field="none",
                relation_to_existing="new_item",
                should_append=True,
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="你帮我整理下，今天审了三个合同，没问题，明天开庭",
        source="test",
    )

    assert result.report_saved is True
    assert result.today_work == ["跟进恒大项目保全资料补充情况", "审了三个合同"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["明天开庭"]


@pytest.mark.asyncio
async def test_pending_quality_full_report_is_reparsed_without_polluting_today_work(monkeypatch):
    existing = _make_report(
        today_work=["跟进恒大项目保全资料补充情况"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天开庭"],
        section_status={
            "today_work": True,
            "problems": True,
            "tomorrow_plan": True,
            "_pending_quality_clarification": {
                "target_field": "today_work",
                "target_index": 0,
                "target_text": "处理恒大事务",
                "clarification_question": "具体处理了恒大哪些事务？例如项目名称、案件或合同事项？",
                "quality_warning": "缺少具体对象和动作结果",
            },
        },
        completeness_score=1.0,
    )
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        outputs=[
            StructuredDailyReport(
                today_work=["处理恒大事务"],
                problems=["暂无明显问题"],
                tomorrow_plan=["明天开庭"],
                completeness=1.0,
            )
        ],
        intent_outputs=[
            _decision(
                target_field="today_work",
                relation_to_existing="semantic_duplicate",
                matched_field="today_work",
                matched_item_index=1,
                should_update_report=True,
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="你帮我整理下，今天处理恒大事务，没问题，明天开庭",
        source="test",
    )

    all_today = "\n".join(result.today_work)
    assert result.report_saved is True
    assert result.today_work == ["跟进恒大项目保全资料补充情况"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["明天开庭"]
    assert "你帮我整理下" not in all_today
    assert "没问题" not in all_today
    assert "明天开庭" not in all_today
    assert "（" not in all_today
    assert "_pending_quality_clarification" not in result.section_status
    assert extractor.raw_inputs == ["今天处理恒大事务，没问题，明天开庭"]


@pytest.mark.asyncio
async def test_pending_edit_full_report_is_reparsed_without_using_whole_sentence_as_replacement(monkeypatch):
    existing = _make_report(
        today_work=["跟进恒大项目保全资料补充情况"],
        problems=[],
        tomorrow_plan=[],
        section_status={
            "today_work": True,
            "problems": False,
            "tomorrow_plan": False,
            "_pending_draft_edit": {
                "pending_action": "edit_draft_item",
                "operation": "rewrite_item",
                "target_field": "today_work",
                "item_refs": [1],
                "target_index": 0,
                "target_indices": [0],
            },
        },
    )
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        outputs=[
            StructuredDailyReport(
                today_work=["处理恒大事务"],
                problems=["暂无明显问题"],
                tomorrow_plan=["明天开庭"],
                completeness=1.0,
            )
        ],
        intent_outputs=[
            _decision(
                target_field="today_work",
                relation_to_existing="semantic_duplicate",
                matched_field="today_work",
                matched_item_index=1,
                should_update_report=True,
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="你帮我整理下，今天处理恒大事务，没问题，明天开庭",
        source="test",
    )

    assert result.today_work == ["跟进恒大项目保全资料补充情况"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["明天开庭"]
    assert "_pending_draft_edit" not in result.section_status
    assert "你帮我整理下" not in "\n".join(result.today_work)


@pytest.mark.asyncio
async def test_pending_quality_short_answer_still_updates_original_item(monkeypatch):
    existing = _make_report(
        today_work=["处理恒大事务"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天开庭"],
        section_status={
            "today_work": True,
            "problems": True,
            "tomorrow_plan": True,
            "_pending_quality_clarification": {
                "target_field": "today_work",
                "target_index": 0,
                "target_text": "处理恒大事务",
                "clarification_question": "具体处理了恒大哪些事务？",
                "quality_warning": "缺少具体对象和动作结果",
            },
        },
        completeness_score=1.0,
    )
    _install_store(monkeypatch, existing)
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor())

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="恒大项目保全资料补充情况", source="test")

    assert result.report_saved is True
    assert result.today_work == ["处理恒大事务（恒大项目保全资料补充情况）"]
    assert "_pending_quality_clarification" not in result.section_status


@pytest.mark.asyncio
async def test_pending_quality_decline_does_not_write_decline_text(monkeypatch):
    existing = _make_report(
        today_work=["处理恒大事务"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天开庭"],
        section_status={
            "today_work": True,
            "problems": True,
            "tomorrow_plan": True,
            "_pending_quality_clarification": {
                "target_field": "today_work",
                "target_index": 0,
                "target_text": "处理恒大事务",
                "clarification_question": "具体处理了恒大哪些事务？",
                "quality_warning": "缺少具体对象和动作结果",
            },
        },
        quality_warning="缺少具体对象和动作结果",
        completeness_score=1.0,
    )
    _install_store(monkeypatch, existing)
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), FakeExtractor())

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="不补充", source="test")

    assert result.today_work == ["处理恒大事务"]
    assert "不补充" not in "\n".join(result.today_work)
    assert "_pending_quality_clarification" not in result.section_status


@pytest.mark.asyncio
async def test_plain_full_report_strips_instruction_prefix_before_extraction(monkeypatch):
    _install_store(monkeypatch)
    extractor = FakeExtractor(
        outputs=[
            StructuredDailyReport(
                today_work=["审了三个合同"],
                problems=["暂无明显问题"],
                tomorrow_plan=["明天开庭"],
                completeness=1.0,
            )
        ],
        intent_outputs=[_decision(target_field="none")],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="你帮我整理下，今天审了三个合同，没问题，明天开庭",
        source="test",
    )

    assert result.today_work == ["审了三个合同"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["明天开庭"]
    assert extractor.raw_inputs == ["今天审了三个合同，没问题，明天开庭"]


@pytest.mark.asyncio
async def test_full_report_semantic_duplicate_today_work_is_not_added_twice(monkeypatch):
    existing = _make_report(today_work=["跟进恒大项目保全资料补充情况"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        outputs=[
            StructuredDailyReport(
                today_work=["处理恒大事务"],
                problems=["暂无明显问题"],
                tomorrow_plan=["明天开庭"],
                completeness=1.0,
            )
        ],
        intent_outputs=[
            _decision(
                target_field="today_work",
                relation_to_existing="semantic_duplicate",
                matched_field="today_work",
                matched_item_index=1,
                should_update_report=True,
            )
        ],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(
        FakeSession(),
        user=_user(),
        raw_input="你帮我整理下，今天处理恒大事务，没问题，明天开庭",
        source="test",
    )

    assert result.today_work == ["跟进恒大项目保全资料补充情况"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["明天开庭"]


@pytest.mark.asyncio
async def test_duplicate_relation_does_not_cross_fill_missing_slot(monkeypatch):
    existing = _make_report(today_work=["今天吃了手抓饼"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing, fail_on_upsert=True)
    extractor = FakeExtractor(
        intent_outputs=[
            _decision(
                relation_to_existing="duplicate",
                matched_field="today_work",
                matched_item_index=1,
                should_update_report=False,
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="今天吃了手抓饼", source="test")

    assert result.report_saved is False
    assert result.today_work == ["今天吃了手抓饼"]
    assert result.problems == []
    assert result.reply_kind == "relation_duplicate"
    assert extractor.extract_calls == 0


@pytest.mark.asyncio
async def test_semantic_duplicate_relation_does_not_cross_fill_missing_slot(monkeypatch):
    existing = _make_report(today_work=["学习了公司规章制度"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing, fail_on_upsert=True)
    extractor = FakeExtractor(
        intent_outputs=[
            _decision(
                relation_to_existing="semantic_duplicate",
                matched_field="today_work",
                matched_item_index=1,
                should_update_report=False,
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="熟悉了公司管理制度", source="test")

    assert result.report_saved is False
    assert result.problems == []
    assert result.reply_kind == "relation_duplicate"


@pytest.mark.asyncio
async def test_elaboration_relation_updates_original_item_not_problems(monkeypatch):
    existing = _make_report(today_work=["处理恒大事务"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        intent_outputs=[
            _decision(
                relation_to_existing="elaboration",
                matched_field="today_work",
                matched_item_index=1,
                new_content="处理恒大事务，并和项目部确认资料缺口",
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="处理恒大事务，并和项目部确认了资料缺口", source="test")

    assert result.report_saved is True
    assert result.today_work == ["处理恒大事务，并和项目部确认资料缺口"]
    assert result.problems == []
    assert extractor.extract_calls == 0


@pytest.mark.asyncio
async def test_elaboration_relation_accepts_zero_index_when_only_one_item(monkeypatch):
    existing = _make_report(today_work=["处理恒大事务"], problems=[], tomorrow_plan=[])
    _install_store(monkeypatch, existing)
    extractor = FakeExtractor(
        intent_outputs=[
            _decision(
                relation_to_existing="elaboration",
                matched_field="today_work",
                matched_item_index=0,
                new_content="处理恒大事务，并和项目部确认了资料缺口",
            )
        ]
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="处理恒大事务，并和项目部确认了资料缺口", source="test")

    assert result.report_saved is True
    assert result.today_work == ["处理恒大事务，并和项目部确认了资料缺口"]
    assert result.problems == []


@pytest.mark.asyncio
async def test_same_sentence_is_not_written_to_multiple_fields(monkeypatch):
    _install_store(monkeypatch)
    extractor = FakeExtractor(
        outputs=[
            StructuredDailyReport(
                today_work=["吃了手抓饼"],
                problems=["吃了手抓饼"],
                completeness=0.67,
            )
        ],
        intent_outputs=[_decision(target_field="today_work")],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="吃了手抓饼", source="test")

    assert result.today_work == ["吃了手抓饼"]
    assert result.problems == []


@pytest.mark.asyncio
async def test_structured_three_field_input_still_extracts_all_fields(monkeypatch):
    _install_store(monkeypatch)
    extractor = FakeExtractor(
        outputs=[
            StructuredDailyReport(
                today_work=["处理合同"],
                problems=["暂无明显问题"],
                tomorrow_plan=["明天开庭"],
                completeness=1.0,
            )
        ],
        intent_outputs=[_decision(target_field="none")],
    )
    service = DailyReportService(SimpleNamespace(timezone="Asia/Shanghai"), extractor)

    result = await service.submit_text(FakeSession(), user=_user(), raw_input="今天处理合同，没问题，明天开庭", source="test")

    assert result.today_work == ["处理合同"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["明天开庭"]
