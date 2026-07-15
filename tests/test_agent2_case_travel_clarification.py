from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agent2.case_table_rag import (
    CaseTableDocument,
    find_case_location_hint_by_identity,
    write_case_table_index,
)
from app.agent2.case_travel_clarification import (
    CaseTravelClarificationPending,
    CaseTravelClarificationRuntime,
    CaseTravelClarificationScope,
    InMemoryCaseTravelClarificationStore,
    build_case_travel_clarification,
    resolve_case_travel_clarification_answer,
)


def test_resolved_case_reads_its_own_accepting_court_without_fuzzy_cross_case_match(
    tmp_path,
) -> None:
    index = tmp_path / "case_index.sqlite"
    write_case_table_index(
        (
            CaseTableDocument(
                doc_id="qujiang",
                source_type="case_table",
                source_file="被告案件底表.xlsx",
                sheet_name="被告案件明细",
                row_number=386,
                table_type="defendant_case",
                case_name="股份六分曲江国际中小学项目李国付运输合同纠纷",
                text="曲江国际 西安铁路运输法院",
                facts={
                    "案件编号": "BGGL-2606-0008",
                    "案件名称": "股份六分曲江国际中小学项目李国付运输合同纠纷",
                    "受理_机构名称": "西安铁路运输法院",
                },
            ),
            CaseTableDocument(
                doc_id="other",
                source_type="case_table",
                source_file="被告案件底表.xlsx",
                sheet_name="被告案件明细",
                row_number=100,
                table_type="defendant_case",
                case_name="股份六分流光云谷创意产业园项目合同纠纷",
                text="流光云谷 曲江 苏州仲裁委员会",
                facts={
                    "案件编号": "BGGL-OTHER",
                    "仲裁委员会": "苏州仲裁委员会",
                },
            ),
        ),
        sqlite_path=index,
    )

    hint = find_case_location_hint_by_identity(
        index,
        case_number="BGGL-2606-0008",
        case_name="股份六分曲江国际中小学项目李国付运输合同纠纷",
        source_id="被告案件底表.xlsx#被告案件明细!386",
    )

    assert hint is not None
    assert hint.case_name == "股份六分曲江国际中小学项目李国付运输合同纠纷"
    assert hint.court_or_location == "西安铁路运输法院"
    assert hint.source_id == "qujiang"


def _pending() -> CaseTravelClarificationPending:
    return CaseTravelClarificationPending(
        pending_id="pending-1",
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        case_id="case-1",
        case_version=3,
        case_name="曲江国际案",
        source_message_id="message-1",
        raw_text="曲江国际明天会去法院和法官当面沟通",
        travel_date="2026-07-15",
        purpose_summary="曲江国际明天会去法院和法官当面沟通",
        suggested_destination="西安铁路运输法院",
        status="awaiting_confirmation",
        version=1,
    )


def test_case_court_visit_creates_one_confirmation_that_preserves_case_date_and_purpose() -> None:
    offer = build_case_travel_clarification(
        raw_text="曲江国际明天会去法院和法官当面沟通",
        case_id="case-1",
        case_version=3,
        case_name="曲江国际案",
        suggested_destination="西安铁路运输法院",
        occurred_at=datetime(2026, 7, 14, 17, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert offer is not None
    assert offer.travel_date == "2026-07-15"
    assert offer.case_id == "case-1"
    assert offer.purpose_summary == "曲江国际明天会去法院和法官当面沟通"
    assert offer.question == (
        "底表显示受理机构为西安铁路运输法院。你明天是去这里吗？"
        "确认后我再登记出差。"
    )


def test_named_alternative_court_replaces_only_destination_and_keeps_original_context() -> None:
    resolution = resolve_case_travel_clarification_answer(
        _pending(),
        "不是，去南京市中级人民法院",
    )

    assert resolution.status == "ready"
    assert resolution.destination_raw == "南京市中级人民法院"
    assert resolution.destination_normalized == "南京市"
    assert resolution.city_code == "320100"
    assert resolution.case_id == "case-1"
    assert resolution.travel_date == "2026-07-15"
    assert resolution.purpose_summary == "曲江国际明天会去法院和法官当面沟通"


def test_generic_other_court_keeps_pending_and_requests_exact_name_without_write() -> None:
    resolution = resolve_case_travel_clarification_answer(
        _pending(),
        "不是，是另一个法院",
    )

    assert resolution.status == "needs_destination"
    assert resolution.actual_write is False
    assert resolution.question == "好的，具体是哪个法院？我先不登记出差。"


def test_confirmed_xian_court_can_resolve_to_travel_city() -> None:
    resolution = resolve_case_travel_clarification_answer(_pending(), "是的")

    assert resolution.status == "ready"
    assert resolution.destination_raw == "西安铁路运输法院"
    assert resolution.destination_normalized == "西安市"
    assert resolution.city_code == "610100"


def test_city_answer_completes_a_corrected_court_without_losing_court_name() -> None:
    pending = CaseTravelClarificationPending(
        **{
            **_pending().__dict__,
            "status": "awaiting_city",
            "candidate_destination": "碑林区人民法院",
        }
    )

    resolution = resolve_case_travel_clarification_answer(pending, "西安")

    assert resolution.status == "ready"
    assert resolution.destination_raw == "碑林区人民法院"
    assert resolution.destination_normalized == "西安市"
    assert resolution.city_code == "610100"


class _Writer:
    def __init__(self) -> None:
        self.calls = []

    async def register(self, pending, resolution, scope):
        self.calls.append((pending, resolution, scope))
        return SimpleNamespace(
            status="executed",
            actual_write=True,
            receipt_id="00000000-0000-0000-0000-000000000099",
        )


def _scope(*, user_id: str = "user-1", message_id: str = "reply-1"):
    return CaseTravelClarificationScope(
        tenant_id="tenant-1",
        user_id=user_id,
        conversation_id="conversation-1",
        source_message_id=message_id,
        occurred_at=datetime(2026, 7, 14, 17, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


@pytest.mark.asyncio
async def test_runtime_consumes_pending_only_after_receipt_backed_travel_write() -> None:
    store = InMemoryCaseTravelClarificationStore((_pending(),))
    writer = _Writer()
    runtime = CaseTravelClarificationRuntime(store=store, writer=writer)

    result = await runtime.handle_reply(
        scope=_scope(),
        raw_text="不是，去南京市中级人民法院",
        allowed_case_ids=("case-1",),
    )

    assert result is not None and result.handled is True
    assert result.status == "registered"
    assert result.receipt is not None
    assert len(writer.calls) == 1
    saved = (await store.list_active(_scope(message_id="inspect")))[0:]
    assert saved == ()
    consumed = store.items["pending-1"]
    assert consumed.status == "consumed"
    assert consumed.case_id == "case-1"
    assert consumed.travel_date == "2026-07-15"
    assert consumed.purpose_summary == "曲江国际明天会去法院和法官当面沟通"


@pytest.mark.asyncio
async def test_runtime_keeps_context_when_user_only_says_another_court() -> None:
    store = InMemoryCaseTravelClarificationStore((_pending(),))
    writer = _Writer()
    runtime = CaseTravelClarificationRuntime(store=store, writer=writer)

    result = await runtime.handle_reply(
        scope=_scope(),
        raw_text="不是，是另一个法院",
        allowed_case_ids=("case-1",),
    )

    assert result is not None and result.status == "needs_destination"
    assert writer.calls == []
    retained = store.items["pending-1"]
    assert retained.status == "awaiting_destination"
    assert retained.case_id == "case-1"
    assert retained.travel_date == "2026-07-15"


@pytest.mark.asyncio
async def test_runtime_does_not_expose_another_users_pending() -> None:
    store = InMemoryCaseTravelClarificationStore((_pending(),))
    writer = _Writer()
    runtime = CaseTravelClarificationRuntime(store=store, writer=writer)

    result = await runtime.handle_reply(
        scope=_scope(user_id="user-2"),
        raw_text="确认",
        allowed_case_ids=("case-1",),
    )

    assert result is None
    assert writer.calls == []


@pytest.mark.asyncio
async def test_confirmation_is_blocked_when_another_pending_is_active() -> None:
    pending = _pending()
    writer = _Writer()
    runtime = CaseTravelClarificationRuntime(
        store=InMemoryCaseTravelClarificationStore((pending,)),
        writer=writer,
    )

    result = await runtime.handle_reply(
        scope=_scope(message_id="reply-with-two-pendings"),
        raw_text="确认",
        allowed_case_ids=(pending.case_id,),
        other_active_pending_count=1,
    )

    assert result is not None
    assert result.status == "ambiguous"
    assert "不确定你在确认哪件事" in result.reply
    assert writer.calls == []
