from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import reports

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


class _Session:
    async def commit(self):
        return None


@pytest.mark.parametrize(
    "untrusted_source",
    ("agent2-force-primary", "legacy", "agent1", "disable_agent2"),
)
@pytest.mark.asyncio
async def test_public_manual_request_source_never_reaches_agent2_route_decision(
    monkeypatch: pytest.MonkeyPatch,
    untrusted_source: str,
) -> None:
    user = SimpleNamespace(
        id="api-user",
        dingtalk_user_id="ding-user",
        timezone="Asia/Shanghai",
    )
    captured_kwargs = []

    async def get_user(*args, **kwargs):
        return user

    async def submit_agent2(**kwargs):
        captured_kwargs.append(kwargs)
        return {"report_id": None, "reply_kind": "trusted_route"}

    monkeypatch.setattr(reports, "get_active_user_by_dingtalk_id", get_user)
    monkeypatch.setattr(reports, "_submit_manual_tool_call_agent2", submit_agent2)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                report_service=SimpleNamespace(
                    extractor=SimpleNamespace(client=object())
                )
            )
        )
    )

    response = await reports.submit_manual_report(
        request,  # type: ignore[arg-type]
        reports.ManualReportRequest(
            dingtalk_user_id="ding-user",
            raw_input="write a daily report",
            source=untrusted_source,
        ),
        _Session(),  # type: ignore[arg-type]
    )

    assert response["reply_kind"] == "trusted_route"
    assert len(captured_kwargs) == 1
    assert "source" not in captured_kwargs[0]


@pytest.mark.asyncio
async def test_manual_api_uses_the_same_direct_tool_call_runtime_as_dingtalk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    report_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        dingtalk_user_id="ding-user",
        name="Test User",
        timezone="Asia/Shanghai",
    )
    report = SimpleNamespace(
        id=report_id,
        user_id=user_id,
        report_date=date.today(),
        status="pending_confirmation",
        today_work=["完成合同复核"],
        problems=[],
        tomorrow_plan=["继续跟进"],
        section_status={"problems_acknowledged_empty": True},
        confirmation_type="none",
        confirmed_by_user=False,
        quality_warning=None,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        reports,
        "get_settings",
        lambda: SimpleNamespace(timezone="Asia/Shanghai"),
    )

    async def process(*args, **kwargs):
        captured.update(kwargs)
        return reports.CanaryIngressOutcome(
            owner="tool_call_core",
            reason="allowed",
            message="已保存。",
            report_id=str(report_id),
            handled=True,
            actual_write=True,
            messages_enabled=True,
            user_visible_result="success",
            reply_formed=True,
        )

    async def get_current_report(*args, **kwargs):
        return report

    monkeypatch.setattr(reports, "process_tool_call_canary_ingress", process)
    monkeypatch.setattr(reports, "get_report", get_current_report)
    session = _Session()

    async def session_get(model, object_id):
        assert model is reports.DailyReport
        return report if object_id == report_id else None

    session.get = session_get  # type: ignore[attr-defined]

    response = await reports._submit_manual_tool_call_agent2(
        session=session,  # type: ignore[arg-type]
        user=user,
        raw_input="今天完成合同复核",
        report_date=None,
        llm_client=object(),
        message_id="manual-message-1",
        conversation_id="conversation-1",
    )

    assert captured["source_channel"] == "manual_text"
    assert captured["conversation_kind"] == "direct"
    assert captured["user_text"] == "今天完成合同复核"
    assert response["reply_kind"] == "agent2_tool_call"
    assert response["actual_write"] is True
    assert response["user_visible_result"] == "success"
    assert response["outcome_reason"] == "allowed"
    assert response["merged_report"]["today_work"] == ["完成合同复核"]


@pytest.mark.asyncio
async def test_manual_api_returns_the_exact_daily_report_written_by_agent2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    today_report = SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        report_date=date(2026, 8, 21),
        status="collecting",
        today_work=["今天原有内容"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
        confirmation_type="none",
        confirmed_by_user=False,
        quality_warning=None,
    )
    yesterday_report = SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        report_date=date(2026, 8, 20),
        status="pending_confirmation",
        today_work=["补写昨天的合同复核"],
        problems=[],
        tomorrow_plan=["昨天计划的后续"],
        section_status={},
        confirmation_type="none",
        confirmed_by_user=False,
        quality_warning=None,
    )
    user = SimpleNamespace(
        id=user_id,
        dingtalk_user_id="ding-user",
        timezone="Asia/Shanghai",
    )
    session = _Session()

    async def session_get(model, object_id):
        assert model is reports.DailyReport
        return (
            yesterday_report
            if object_id == yesterday_report.id
            else None
        )

    session.get = session_get  # type: ignore[attr-defined]
    monkeypatch.setattr(
        reports,
        "get_settings",
        lambda: SimpleNamespace(timezone="Asia/Shanghai"),
    )

    async def process(*args, **kwargs):
        return reports.CanaryIngressOutcome(
            owner="tool_call_core",
            reason="allowed",
            message="昨天的日报已补写。",
            report_id=str(yesterday_report.id),
            handled=True,
            actual_write=True,
            messages_enabled=True,
            user_visible_result="success",
            reply_formed=True,
        )

    async def get_fallback(*args, **kwargs):
        return today_report

    monkeypatch.setattr(reports, "process_tool_call_canary_ingress", process)
    monkeypatch.setattr(reports, "get_report", get_fallback)

    response = await reports._submit_manual_tool_call_agent2(
        session=session,  # type: ignore[arg-type]
        user=user,
        raw_input="补写昨天：完成合同复核",
        report_date=None,
        llm_client=object(),
        message_id="manual-history-write",
        conversation_id="conversation-1",
    )

    assert response["report_id"] == str(yesterday_report.id)
    assert response["report_date"] == "2026-08-20"
    assert response["merged_report"]["today_work"] == [
        "补写昨天的合同复核"
    ]


@pytest.mark.asyncio
async def test_manual_api_does_not_allow_a_hidden_report_date_to_override_agent2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid4(), dingtalk_user_id="ding-user")

    async def get_user(*args, **kwargs):
        return user

    monkeypatch.setattr(reports, "get_active_user_by_dingtalk_id", get_user)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(HTTPException) as exc_info:
        await reports.submit_manual_report(
            request=request,
            body=reports.ManualReportRequest(
                dingtalk_user_id="ding-user",
                raw_input="完成合同复核",
                report_date=date(2026, 8, 20),
            ),
            session=_Session(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert "日期写进 raw_input" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_manual_api_preserves_partial_failure_even_when_a_write_happened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    report_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        dingtalk_user_id="ding-user",
        timezone="Asia/Shanghai",
    )
    report = SimpleNamespace(
        id=report_id,
        user_id=user_id,
        report_date=date.today(),
        status="collecting",
        today_work=["已安全写入的一项"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
        confirmation_type="none",
        confirmed_by_user=False,
        quality_warning=None,
    )
    session = _Session()

    async def session_get(model, object_id):
        return report if object_id == report_id else None

    session.get = session_get  # type: ignore[attr-defined]
    monkeypatch.setattr(
        reports,
        "get_settings",
        lambda: SimpleNamespace(timezone="Asia/Shanghai"),
    )

    async def process(*args, **kwargs):
        return reports.CanaryIngressOutcome(
            owner="tool_call_core",
            reason="partial_clarification",
            message="一项已记录，另一项还需要你确认具体日期。",
            report_id=str(report_id),
            handled=True,
            actual_write=True,
            messages_enabled=True,
            user_visible_result="clarification",
            reply_formed=True,
        )

    monkeypatch.setattr(reports, "process_tool_call_canary_ingress", process)

    response = await reports._submit_manual_tool_call_agent2(
        session=session,  # type: ignore[arg-type]
        user=user,
        raw_input="记录一项并确认另一项",
        report_date=None,
        llm_client=object(),
        message_id="partial-write",
        conversation_id="conversation-1",
    )

    assert response["reply_kind"] == "agent2_tool_call_clarification"
    assert response["actual_write"] is True
    assert response["user_visible_result"] == "clarification"


@pytest.mark.parametrize("trusted_route", ("agent1", "agent2_shadow"))
@pytest.mark.asyncio
async def test_historical_non_primary_manual_route_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    trusted_route: str,
) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Shanghai",
        agent2_business_phase2_enabled=True,
        agent2_daily_enabled=False,
        agent2_daily_enabled_user_ids="",
    )
    user = SimpleNamespace(
        id="api-user",
        dingtalk_user_id="ding-user",
        name="Test User",
        timezone="Asia/Shanghai",
    )
    resolution = SimpleNamespace(
        decision=SimpleNamespace(route=trusted_route),
        binding=None,
    )

    monkeypatch.setattr(reports, "get_settings", lambda: settings)

    async def resolve(*args, **kwargs):
        return resolution

    monkeypatch.setattr(reports, "resolve_agent2_entrypoint", resolve)

    response = await reports._submit_manual_agent2_if_applicable(
        session=_Session(),  # type: ignore[arg-type]
        user=user,
        raw_input="write a daily report",
        llm_client=None,
        message_id=f"manual-{trusted_route}",
        conversation_id="conversation-1",
        report_date=None,
    )

    assert response is not None
    assert response["reply_kind"] == "agent2_entrypoint_blocked"


@pytest.mark.asyncio
async def test_ambiguous_trusted_manual_route_fails_closed_before_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Shanghai",
        agent2_business_phase2_enabled=True,
        agent2_daily_enabled=False,
        agent2_daily_enabled_user_ids="",
    )
    user = SimpleNamespace(
        id="api-user",
        dingtalk_user_id="ding-user",
        name="Test User",
        timezone="Asia/Shanghai",
    )
    resolution = SimpleNamespace(
        decision=SimpleNamespace(route="blocked"),
        binding=None,
    )

    monkeypatch.setattr(reports, "get_settings", lambda: settings)

    async def resolve(*args, **kwargs):
        return resolution

    async def no_daily_task(*args, **kwargs):
        return None

    async def no_report(*args, **kwargs):
        return None

    monkeypatch.setattr(reports, "resolve_agent2_entrypoint", resolve)
    monkeypatch.setattr(reports, "build_live_daily_active_task", no_daily_task)
    monkeypatch.setattr(reports, "get_report", no_report)

    response = await reports._submit_manual_agent2_if_applicable(
        session=_Session(),  # type: ignore[arg-type]
        user=user,
        raw_input="write a daily report",
        llm_client=None,
        message_id="manual-ambiguous",
        conversation_id="conversation-1",
        report_date=None,
    )

    assert response is not None
    assert response["reply_kind"] == "agent2_entrypoint_blocked"


@pytest.mark.asyncio
async def test_primary_manual_route_cannot_be_disabled_by_request_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the trusted route control may roll a Manual turn back from Agent2."""

    settings = SimpleNamespace(
        timezone="Asia/Shanghai",
        agent2_business_phase2_enabled=True,
        agent2_daily_enabled=False,
        agent2_daily_enabled_user_ids="",
    )
    user = SimpleNamespace(
        id="api-user",
        dingtalk_user_id="ding-user",
        name="Test User",
        timezone="Asia/Shanghai",
    )
    binding = SimpleNamespace(user_id="bound-user")
    resolution = SimpleNamespace(
        decision=SimpleNamespace(route="agent2_primary"),
        binding=binding,
    )
    captured_channels: list[str] = []

    monkeypatch.setattr(reports, "get_settings", lambda: settings)

    async def resolve(*args, **kwargs):
        return resolution

    def build_context(*args, **kwargs):
        captured_channels.append(kwargs["source_channel"])
        return SimpleNamespace(
            tenant_id="tenant-canary",
            actor_user_id="bound-user",
            conversation_id="conversation-1",
            occurred_at=NOW,
        )

    async def no_daily_task(*args, **kwargs):
        return None

    async def no_report(*args, **kwargs):
        return None

    monkeypatch.setattr(reports, "resolve_agent2_entrypoint", resolve)
    monkeypatch.setattr(reports, "build_business_command_context", build_context)
    monkeypatch.setattr(reports, "build_live_daily_active_task", no_daily_task)
    monkeypatch.setattr(reports, "get_report", no_report)
    monkeypatch.setattr(reports, "cognitive_core_v3_enabled", lambda value: False)

    response = await reports._submit_manual_agent2_if_applicable(
        session=_Session(),  # type: ignore[arg-type]
        user=user,
        raw_input="write a daily report",
        llm_client=None,
        message_id="manual-message-1",
        conversation_id="conversation-1",
        report_date=None,
    )

    assert response is not None
    assert response["reply_kind"] == "agent2_cognitive_core_disabled"
    assert captured_channels == ["manual_text"]


@pytest.mark.asyncio
async def test_enforced_manual_read_only_turn_uses_runtime_outcome_without_shadow_or_second_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted no-command turn has one semantic authority and zero writes."""

    settings = SimpleNamespace(
        timezone="Asia/Shanghai",
        agent2_business_phase2_enabled=True,
        agent2_daily_enabled=False,
        agent2_daily_enabled_user_ids="",
    )
    user = SimpleNamespace(
        id="api-user",
        dingtalk_user_id="ding-user",
        name="Test User",
        timezone="Asia/Shanghai",
    )
    binding = SimpleNamespace(user_id="bound-user")
    resolution = SimpleNamespace(
        decision=SimpleNamespace(route="agent2_primary"),
        binding=binding,
    )
    business_context = SimpleNamespace(
        tenant_id="tenant-canary",
        actor_user_id="bound-user",
        conversation_id="conversation-1",
        occurred_at=NOW,
    )
    decision = SimpleNamespace(
        admission_mode="enforced",
        admission_selection_requests=(),
        clarification_need=None,
    )
    orchestration = SimpleNamespace(
        decision=decision,
        command_plan=SimpleNamespace(
            report_commands=(),
            business_commands=(),
            daily_commands=(),
        ),
    )
    runtime_result = SimpleNamespace(
        orchestration=orchestration,
        business_execution_context=business_context,
        mutation_execution_authority=None,
        selection_continuation=None,
    )
    persisted = []
    runtime_calls = []

    class _Runtime:
        async def handle(self, request):
            runtime_calls.append(request)
            return runtime_result

    class _NoSecondLlm:
        async def complete_json(self, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("Manual Enforce invoked a second LLM authority")

    monkeypatch.setattr(reports, "get_settings", lambda: settings)

    async def resolve(*args, **kwargs):
        return resolution

    async def no_daily_task(*args, **kwargs):
        return None

    async def no_report(*args, **kwargs):
        return None

    async def persist(*args, **kwargs):
        persisted.extend(args[1])
        return tuple(args[1])

    def no_shadow(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("Manual Enforce invoked the legacy Shadow interpreter")

    monkeypatch.setattr(reports, "resolve_agent2_entrypoint", resolve)
    monkeypatch.setattr(
        reports,
        "build_business_command_context",
        lambda *args, **kwargs: business_context,
    )
    monkeypatch.setattr(reports, "build_live_daily_active_task", no_daily_task)
    monkeypatch.setattr(reports, "get_report", no_report)
    monkeypatch.setattr(reports, "cognitive_core_v3_enabled", lambda value: True)
    monkeypatch.setattr(reports, "production_agent2_turn_runtime", lambda: _Runtime())
    monkeypatch.setattr(reports, "evaluate_daily_shadow", no_shadow)
    monkeypatch.setattr(reports, "persist_operation_outcomes", persist)

    response = await reports._submit_manual_agent2_if_applicable(
        session=_Session(),  # type: ignore[arg-type]
        user=user,
        raw_input="hello",
        llm_client=_NoSecondLlm(),
        message_id="manual-message-2",
        conversation_id="conversation-1",
        report_date=None,
    )

    assert len(runtime_calls) == 1
    assert response is not None
    assert response["reply_kind"] == "agent2_read_only"
    assert len(persisted) == 1
    assert persisted[0].domain == "chat"
    assert persisted[0].actual_write is False
