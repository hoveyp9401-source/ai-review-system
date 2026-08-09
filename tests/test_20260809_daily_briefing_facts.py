from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.daily_briefing_fact_query import (
    DailyBriefingFactEvent,
    DailyBriefingFactQuery,
    DailyBriefingFactQueryRequest,
    DailyBriefingFactSource,
)
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    QueryDailyBriefingFactsArgs,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.daily_briefing_reply import (
    render_daily_briefing_reply,
    validate_daily_briefing_reply,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekResponseError,
    DeepSeekToolCallingAdapter,
    _CompletionResponse,
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.tool_calling.production_daily_executor import (
    ProductionDailyExecutor,
)
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardRecords,
    MemberRecord,
    SubmissionObligation,
    TeamRecord,
)
from app.scheduler.runner import _send_daily_briefings
from app.services.management_daily_briefing import (
    BriefingRecipient,
    build_management_daily_briefings,
)

REPORT_DATE = date(2026, 8, 7)
GENERATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)
LEGACY_LIMIT = (
    "该历史晨报没有保存生成时的结构化成员分类快照，"
    "不能仅凭当前日报状态反推当时原因。"
)


class _Repository:
    def __init__(self, source: DailyBriefingFactSource) -> None:
        self.source = source
        self.calls: list[tuple[str, date]] = []

    async def load_source(
        self,
        *,
        tenant_id: str,
        report_date: date,
    ) -> DailyBriefingFactSource:
        self.calls.append((tenant_id, report_date))
        return self.source


def _member(
    ref: str,
    name: str,
    *,
    team_ref: str = "team-1",
    team_name: str = "综合管理部",
) -> MemberRecord:
    return MemberRecord(
        ref=ref,
        name=name,
        team_ref=team_ref,
        team_name=team_name,
        department_name="法务合约中心",
    )


def _report(
    *,
    member_ref: str,
    status: str = "completed",
    submitted_at: datetime | None = None,
) -> DailyReportRecord:
    return DailyReportRecord(
        ref=f"report-{member_ref}",
        member_ref=member_ref,
        team_ref="team-1",
        report_date=REPORT_DATE,
        status=status,
        confirmation_type="user_confirmed",
        confirmed_by_user=True,
        submitted_at=submitted_at,
    )


def test_management_briefing_carries_the_exact_generation_snapshot() -> None:
    members = (
        _member("member-submitted", "甲成员"),
        _member("member-missing", "乙成员"),
    )
    records = DashboardRecords(
        members=members,
        obligations=tuple(
            SubmissionObligation(
                member_ref=member.ref,
                team_ref=member.team_ref,
                report_date=REPORT_DATE,
                required=True,
                reason="正常工作日",
                deadline_at=GENERATED_AT,
                data_complete=True,
            )
            for member in members
        ),
        reports=(
            _report(
                member_ref="member-submitted",
                submitted_at=GENERATED_AT,
            ),
        ),
    )
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=GENERATED_AT,
        teams=(
            TeamRecord(
                ref="team-1",
                name="综合管理部",
                department_name="法务合约中心",
            ),
        ),
        records=records,
        recipients=(
            BriefingRecipient(
                id="lead-1",
                name="负责人",
                dingtalk_user_id="ding-lead-1",
                role="team_lead",
                team_ref="team-1",
            ),
        ),
    )

    snapshot = briefings["team_messages"][0]["briefing_snapshot"]
    assert snapshot["generated_at"] == GENERATED_AT.isoformat()
    assert snapshot["report_date"] == REPORT_DATE.isoformat()
    assert snapshot["scope"] == "team"
    assert {
        item["member_ref"]: item for item in snapshot["members"]
    } == {
        "member-submitted": {
            "member_ref": "member-submitted",
            "member_name": "甲成员",
            "team_ref": "team-1",
            "team_name": "综合管理部",
            "classification": "submitted",
            "report_status": "completed",
            "confirmation_type": "user_confirmed",
            "submitted_at": GENERATED_AT.isoformat(),
        },
        "member-missing": {
            "member_ref": "member-missing",
            "member_name": "乙成员",
            "team_ref": "team-1",
            "team_name": "综合管理部",
            "classification": "missing",
            "report_status": None,
            "confirmation_type": None,
            "submitted_at": None,
        },
    }


def test_management_snapshot_uses_the_briefing_generation_timezone() -> None:
    generated_at = datetime(
        2026,
        8,
        8,
        9,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    submitted_at = datetime(2026, 8, 8, 0, 30, tzinfo=timezone.utc)
    member = _member("member-submitted", "甲成员")
    records = DashboardRecords(
        members=(member,),
        obligations=(
            SubmissionObligation(
                member_ref=member.ref,
                team_ref=member.team_ref,
                report_date=REPORT_DATE,
                required=True,
                reason="正常工作日",
                deadline_at=generated_at,
                data_complete=True,
            ),
        ),
        reports=(
            _report(
                member_ref=member.ref,
                submitted_at=submitted_at,
            ),
        ),
    )

    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=generated_at,
        teams=(
            TeamRecord(
                ref="team-1",
                name="综合管理部",
                department_name="法务合约中心",
            ),
        ),
        records=records,
        recipients=(
            BriefingRecipient(
                id="lead-1",
                name="负责人",
                dingtalk_user_id="ding-lead-1",
                role="team_lead",
                team_ref="team-1",
            ),
        ),
    )

    snapshot = briefings["team_messages"][0]["briefing_snapshot"]
    assert snapshot["generated_at"] == generated_at.isoformat()
    assert snapshot["members"][0]["submitted_at"] == (
        submitted_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    )


def test_legacy_briefing_query_returns_recorded_text_but_no_invented_cause() -> None:
    submitted_at = GENERATED_AT.replace(hour=9, minute=15)
    source = DailyBriefingFactSource(
        members=(
            _member("member-1", "目标成员"),
            _member("lead-1", "负责人"),
        ),
        reports=(
            _report(member_ref="member-1", submitted_at=submitted_at),
        ),
        events=(
            DailyBriefingFactEvent(
                recipient_ref="lead-1",
                recipient_name="负责人",
                report_date=REPORT_DATE,
                created_at=GENERATED_AT,
                scope="team",
                team_name="综合管理部",
                department_name="",
                message_status="delivered",
                delivery_verified=True,
                message_text="综合管理部晨报：目标成员显示未提交。",
                briefing_snapshot=None,
            ),
        ),
    )
    repository = _Repository(source)
    query = DailyBriefingFactQuery(repository)

    result = asyncio.run(
        query.execute(
            tenant_id="tenant-1",
            actor_user_id="actor-1",
            request=DailyBriefingFactQueryRequest(
                report_date=REPORT_DATE,
                member_name="目标成员",
            ),
            now=GENERATED_AT.replace(hour=16),
            timezone="Asia/Shanghai",
        )
    )

    assert repository.calls == [("tenant-1", REPORT_DATE)]
    assert result["target_member"]["name"] == "目标成员"
    assert result["current_submission"]["status"] == "completed"
    assert result["current_submission"]["submitted_at"] == submitted_at.astimezone(
        ZoneInfo("Asia/Shanghai")
    ).isoformat()
    assert result["recorded_briefings"][0]["sent_at"] == (
        GENERATED_AT.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    )
    assert result["recorded_briefings"][0]["message_text"].endswith(
        "目标成员显示未提交。"
    )
    assert result["recorded_briefings"][0]["snapshot_available"] is False
    assert result["recorded_briefings"][0]["member_at_snapshot"] is None
    assert result["cause"] is None
    assert result["evidence_limits"] == [
        "该历史晨报没有保存生成时的结构化成员分类快照，不能仅凭当前日报状态反推当时原因。"
    ]


def test_structured_briefing_query_preserves_snapshot_and_current_timeline() -> None:
    submitted_at = GENERATED_AT.replace(hour=9, minute=15)
    snapshot = {
        "generated_at": GENERATED_AT.isoformat(),
        "report_date": REPORT_DATE.isoformat(),
        "scope": "team",
        "members": [
            {
                "member_ref": "member-1",
                "member_name": "目标成员",
                "team_ref": "team-1",
                "team_name": "综合管理部",
                "classification": "missing",
                "report_status": None,
                "confirmation_type": None,
                "submitted_at": None,
            }
        ],
    }
    source = DailyBriefingFactSource(
        members=(
            _member("member-1", "目标成员"),
            _member("lead-1", "负责人"),
        ),
        reports=(
            _report(member_ref="member-1", submitted_at=submitted_at),
        ),
        events=(
            DailyBriefingFactEvent(
                recipient_ref="lead-1",
                recipient_name="负责人",
                report_date=REPORT_DATE,
                created_at=GENERATED_AT,
                scope="team",
                team_name="综合管理部",
                department_name="",
                message_status="delivered",
                delivery_verified=True,
                message_text="综合管理部晨报：目标成员显示未提交。",
                briefing_snapshot=snapshot,
            ),
        ),
    )

    result = asyncio.run(
        DailyBriefingFactQuery(_Repository(source)).execute(
            tenant_id="tenant-1",
            actor_user_id="actor-1",
            request=DailyBriefingFactQueryRequest(
                report_date=REPORT_DATE,
                member_name="目标成员",
            ),
            now=GENERATED_AT.replace(hour=16),
            timezone="Asia/Shanghai",
        )
    )

    event = result["recorded_briefings"][0]
    assert event["snapshot_available"] is True
    assert event["snapshot_generated_at"] == GENERATED_AT.astimezone(
        ZoneInfo("Asia/Shanghai")
    ).isoformat()
    assert event["member_at_snapshot"]["classification"] == "missing"
    assert result["current_submission"]["submitted_at"] == submitted_at.astimezone(
        ZoneInfo("Asia/Shanghai")
    ).isoformat()
    assert result["cause"] is None
    assert result["evidence_limits"] == []


def test_agent2_has_a_dedicated_read_only_briefing_fact_tool() -> None:
    definition = TOOL_REGISTRY["query_daily_briefing_facts"]

    assert definition.read_or_write == "read"
    assert definition.transaction_target_policy == "read_only"
    assert "server" in definition.object_binding_policy
    prompt = canary_system_prompt()
    assert "query_daily_briefing_facts" in prompt
    assert "query_report_by_date" in prompt
    assert "merely to recheck" in prompt
    assert "不能仅凭当前日报状态反推" in prompt
    assert "为什么说没交" not in prompt


@pytest.mark.asyncio
async def test_production_executor_forwards_the_principal_timezone() -> None:
    class CapturingFactQuery:
        def __init__(self) -> None:
            self.kwargs = None

        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return {
                "query_kind": "daily_briefing_facts",
                "report_date": REPORT_DATE.isoformat(),
                "timezone": kwargs["timezone"],
                "recorded_briefings": [],
                "current_submission": None,
                "cause": None,
                "evidence_limits": [],
            }

    user_id = UUID("11111111-1111-1111-1111-111111111111")
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=GENERATED_AT,
        principal=TrustedPrincipal(
            tenant_id="tenant-1",
            user_id=user_id,
            conversation_id="conversation",
            source_message_id="message",
            timezone="Asia/Shanghai",
        ),
        business_glossary={
            "conversation_report_date": REPORT_DATE.isoformat(),
        },
        allowed_tool_names=frozenset({"query_daily_briefing_facts"}),
        gate_decisions={"query_daily_briefing_facts": True},
    )
    query = CapturingFactQuery()
    user = SimpleNamespace(
        id=user_id,
        name="测试成员",
        dingtalk_user_id="ding-test",
        team_id=UUID("22222222-2222-2222-2222-222222222222"),
        role="member",
    )
    executor = ProductionDailyExecutor(
        session=object(),
        user=user,
        context=context,
        settings=SimpleNamespace(
            legal_daily_dashboard_tenant_id="tenant-1",
        ),
        bound_calls={},
        source_channel="test",
        source_text_hash="0" * 64,
        date_resolver=object(),
        daily_briefing_fact_query=query,
    )
    request = ProductionHandlerRequest(
        tool_call_id="briefing-call",
        tool_name="query_daily_briefing_facts",
        arguments=QueryDailyBriefingFactsArgs(),
        executor=executor,
        memory_executor=object(),
    )

    outcome = await executor.query_daily_briefing_facts(request)

    assert outcome.status_if_unchanged == ReceiptStatus.SUCCESS
    assert query.kwargs is not None
    assert query.kwargs["timezone"] == "Asia/Shanghai"


def test_briefing_dispatch_persists_the_generation_snapshot_unchanged() -> None:
    snapshot = {
        "generated_at": GENERATED_AT.isoformat(),
        "report_date": REPORT_DATE.isoformat(),
        "scope": "team",
        "members": [
            {
                "member_ref": "member-1",
                "member_name": "目标成员",
                "team_ref": "team-1",
                "team_name": "综合管理部",
                "classification": "missing",
                "report_status": None,
                "confirmation_type": None,
                "submitted_at": None,
            }
        ],
    }

    class Robot:
        async def send_robot_direct_markdown(self, **kwargs):
            return {
                "processQueryKey": "provider-ref",
                "deliveryVerified": True,
                "deliveryStatus": "SUCCESS",
                "deliveryRecipientUserIds": kwargs["user_ids"],
            }

    class Session:
        def __init__(self) -> None:
            self.events = []

        def add(self, event) -> None:
            self.events.append(event)

        async def scalars(self, _query):
            events = self.events

            class Rows:
                def all(self):
                    return list(events)

            return Rows()

    session = Session()
    sent = asyncio.run(
        _send_daily_briefings(
            Robot(),
            {
                "date": REPORT_DATE.isoformat(),
                "team_messages": [
                    {
                        "scope": "team",
                        "team_id": "team-1",
                        "team_name": "综合管理部",
                        "recipients": [
                            {
                                "id": "11111111-1111-1111-1111-111111111111",
                                "name": "负责人",
                                "dingtalk_user_id": "ding-lead-1",
                            }
                        ],
                        "briefing_snapshot": snapshot,
                        "text": "综合管理部晨报",
                    }
                ],
            },
            session=session,
            report_date=REPORT_DATE,
        )
    )

    assert sent == 1
    assert len(session.events) == 1
    assert session.events[0].llm_decision_json["briefing_snapshot"] == snapshot


def _briefing_receipt(
    *,
    snapshot_classification: str | None = None,
) -> ToolReceipt:
    event = {
        "message_text": "记录中的晨报没有把目标成员列为未交。",
        "snapshot_available": snapshot_classification is not None,
    }
    if snapshot_classification is not None:
        event["member_at_snapshot"] = {
            "member_name": "目标成员",
            "classification": snapshot_classification,
        }
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="query_daily_briefing_facts",
        changed=False,
        target_type="daily_briefing_facts",
        target_id=REPORT_DATE.isoformat(),
        safe_user_facts={
            "actual_write": False,
            "daily_briefing_facts": {
                "query_kind": "daily_briefing_facts",
                "report_date": REPORT_DATE.isoformat(),
                "recorded_briefings": [event],
                "current_submission": {"status": "completed"},
                "cause": None,
                "evidence_limits": (
                    [LEGACY_LIMIT]
                    if snapshot_classification is None
                    else []
                ),
            },
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


def test_briefing_reply_contract_requires_limit_and_no_causal_conclusion() -> None:
    invalid = json.dumps(
        {
            "reply": "可能是提交时间接近晨报生成时间。",
            "premise_status": "contradicted",
            "recorded_evidence_quotes": [
                "记录中的晨报没有把目标成员列为未交。"
            ],
            "acknowledged_evidence_limits": [],
            "causal_conclusion": "提交时间接近晨报生成时间",
        },
        ensure_ascii=False,
    )
    envelope, errors = validate_daily_briefing_reply(
        invalid,
        (_briefing_receipt(),),
    )

    assert envelope is None
    assert "evidence_limits_mismatch" in errors
    assert "unsupported_causal_conclusion" in errors

    valid_reply = "记录中的晨报没有把目标成员列为未交。"
    valid = json.dumps(
        {
            "reply": valid_reply,
            "premise_status": "contradicted",
            "recorded_evidence_quotes": [valid_reply],
            "acknowledged_evidence_limits": [LEGACY_LIMIT],
            "causal_conclusion": None,
        },
        ensure_ascii=False,
    )
    envelope, errors = validate_daily_briefing_reply(
        valid,
        (_briefing_receipt(),),
    )

    assert errors == ()
    assert envelope is not None
    assert envelope.reply == valid_reply
    assert render_daily_briefing_reply(envelope) == (
        "系统保存的这份晨报原文与问题中的情况不一致。\n\n"
        f"{valid_reply}\n\n"
        f"系统保存的晨报原文片段：\n“{valid_reply}”\n\n"
        f"{LEGACY_LIMIT}"
    )

    valid_single_quote = json.dumps(
        {
            "reply": valid_reply,
            "premise_status": "contradicted",
            "recorded_evidence_quotes": valid_reply,
            "acknowledged_evidence_limits": [LEGACY_LIMIT],
            "causal_conclusion": None,
        },
        ensure_ascii=False,
    )
    envelope, errors = validate_daily_briefing_reply(
        valid_single_quote,
        (_briefing_receipt(),),
    )
    assert errors == ()
    assert envelope is not None
    assert envelope.recorded_evidence_quotes == (valid_reply,)

    for unsupported_reply in (
        "会不会是后来又有别的统计让你误以为是晨报的结果？",
        "晨报收录了你的工作，说明生成时你的日报已经是已提交状态。",
    ):
        unsupported = json.dumps(
            {
                "reply": unsupported_reply,
                "premise_status": "contradicted",
                "recorded_evidence_quotes": [valid_reply],
                "acknowledged_evidence_limits": [LEGACY_LIMIT],
                "causal_conclusion": None,
            },
            ensure_ascii=False,
        )
        envelope, errors = validate_daily_briefing_reply(
            unsupported,
            (_briefing_receipt(),),
        )
        assert envelope is None
        assert "unsupported_causal_wording" in errors

    invented_quote = json.dumps(
        {
            "reply": valid_reply,
            "premise_status": "contradicted",
            "recorded_evidence_quotes": ["这句并不在晨报原文里。"],
            "acknowledged_evidence_limits": [LEGACY_LIMIT],
            "causal_conclusion": None,
        },
        ensure_ascii=False,
    )
    envelope, errors = validate_daily_briefing_reply(
        invented_quote,
        (_briefing_receipt(),),
    )
    assert envelope is None
    assert "unbound_recorded_evidence_quote" in errors

    legacy_overclaim = json.dumps(
        {
            "reply": "晨报已经将您列为已提交人员。",
            "premise_status": "contradicted",
            "recorded_evidence_quotes": valid_reply,
            "acknowledged_evidence_limits": [LEGACY_LIMIT],
            "causal_conclusion": None,
        },
        ensure_ascii=False,
    )
    envelope, errors = validate_daily_briefing_reply(
        legacy_overclaim,
        (_briefing_receipt(),),
    )
    assert envelope is None
    assert "unsupported_legacy_submission_classification" in errors

    snapshot_supported = json.dumps(
        {
            "reply": "生成快照将目标成员列为已提交。",
            "premise_status": "supported",
            "recorded_evidence_quotes": valid_reply,
            "acknowledged_evidence_limits": [],
            "causal_conclusion": None,
        },
        ensure_ascii=False,
    )
    envelope, errors = validate_daily_briefing_reply(
        snapshot_supported,
        (_briefing_receipt(snapshot_classification="submitted"),),
    )
    assert errors == ()
    assert envelope is not None


@pytest.mark.asyncio
async def test_briefing_fact_batch_closes_tools_and_retries_an_ungrounded_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=GENERATED_AT,
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("11111111-1111-1111-1111-111111111111"),
            conversation_id="conversation",
            source_message_id="message",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"query_daily_briefing_facts"}),
        gate_decisions={"query_daily_briefing_facts": True},
    )

    class RuntimeSession:
        mode = ExecutionMode.CANARY_EXECUTE

        async def execute(self, calls):
            assert len(calls) == 1
            assert calls[0].tool_name == "query_daily_briefing_facts"
            return ProductionRuntimeResult(
                status="success",
                receipts=(_briefing_receipt(),),
                handler_call_count=1,
            )

    valid_reply = "记录中的晨报没有把目标成员列为未交。"
    valid = json.dumps(
        {
            "reply": valid_reply,
            "premise_status": "contradicted",
            "recorded_evidence_quotes": [valid_reply],
            "acknowledged_evidence_limits": [LEGACY_LIMIT],
            "causal_conclusion": None,
        },
        ensure_ascii=False,
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "briefing-facts-1",
                            "type": "function",
                            "function": {
                                "name": "query_daily_briefing_facts",
                                "arguments": json.dumps(
                                    {
                                        "view": "member_classification",
                                        "report_date_expression": "昨天",
                                        "proposed_report_date": REPORT_DATE.isoformat(),
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": "   "},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": valid},
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    calls = []
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=3,
        endpoint="https://example.invalid/chat/completions",
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        calls.append(
            {
                "tool_schema_count": len(tool_schemas),
                "thinking_enabled": thinking_enabled,
                "system_messages": [
                    item["content"]
                    for item in messages
                    if item.get("role") == "system"
                ],
            }
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)
    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="我昨天明明交了日报，为什么晨报说我没交？",
        context=context,
        runtime_session=RuntimeSession(),
    )

    assert result.iterations == 3
    assert result.receipts == (_briefing_receipt(),)
    assert result.final_content == (
        "系统保存的这份晨报原文与问题中的情况不一致。\n\n"
        f"{valid_reply}\n\n"
        f"系统保存的晨报原文片段：\n“{valid_reply}”\n\n"
        f"{LEGACY_LIMIT}"
    )
    assert [item["tool_schema_count"] for item in calls] == [1, 0, 0]
    assert [item["thinking_enabled"] for item in calls] == [
        False,
        True,
        True,
    ]
    assert any(
        "briefing_fact_batch_closed" in message
        for message in calls[1]["system_messages"]
    )
    assert result.model_turns[1].response_metadata[
        "daily_briefing_reply_retry"
    ] is True


@pytest.mark.asyncio
async def test_exhausted_briefing_reply_records_the_final_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=GENERATED_AT,
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("11111111-1111-1111-1111-111111111111"),
            conversation_id="conversation",
            source_message_id="message",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"query_daily_briefing_facts"}),
        gate_decisions={"query_daily_briefing_facts": True},
    )

    class RuntimeSession:
        mode = ExecutionMode.CANARY_EXECUTE

        async def execute(self, calls):
            assert len(calls) == 1
            return ProductionRuntimeResult(
                status="success",
                receipts=(_briefing_receipt(),),
                handler_call_count=1,
            )

    invalid = json.dumps(
        {
            "reply": "晨报原文与问题不一致。",
            "premise_status": "contradicted",
            "recorded_evidence_quotes": {"quote": "不是字符串或列表"},
            "acknowledged_evidence_limits": [LEGACY_LIMIT],
            "causal_conclusion": None,
        },
        ensure_ascii=False,
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "briefing-facts-1",
                            "type": "function",
                            "function": {
                                "name": "query_daily_briefing_facts",
                                "arguments": json.dumps(
                                    {
                                        "view": "member_classification",
                                        "report_date_expression": "昨天",
                                        "proposed_report_date": (
                                            REPORT_DATE.isoformat()
                                        ),
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            *(
                _CompletionResponse(
                    message={"role": "assistant", "content": invalid},
                    metadata={"finish_reason": "stop"},
                )
                for _ in range(3)
            ),
        )
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=3,
        endpoint="https://example.invalid/chat/completions",
    )

    async def fake_complete(_messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(DeepSeekResponseError) as captured:
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="我昨天明明交了日报，为什么晨报说我没交？",
            context=context,
            runtime_session=RuntimeSession(),
        )

    final_metadata = captured.value.model_turns[-1].response_metadata
    assert final_metadata["daily_briefing_reply_validation"] == [
        "invalid_recorded_evidence_quotes",
        "missing_recorded_evidence_quote",
    ]
    assert final_metadata["terminal_reply_validation"] == [
        "invalid_recorded_evidence_quotes",
        "missing_recorded_evidence_quote",
    ]
