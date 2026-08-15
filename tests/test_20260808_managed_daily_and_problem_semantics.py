from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    QueryManagedDailyReportsArgs,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.managed_daily_reply import (
    validate_managed_daily_reply,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
    _CompletionResponse,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionRuntimeResult,
)
from app.agent2.tool_calling.receipt_reply import finalize_canary_content
from app.legal_daily_dashboard.chat_query import (
    ManagedDailyQuery,
    ManagedDailyQueryRequest,
)
from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardActor,
    MemberRecord,
    SubmissionObligation,
    TeamRecord,
)
from app.legal_daily_dashboard.repository import InMemoryDashboardRepository
from app.services.report_risk import problems_acknowledged_empty


REPORT_DATE = date(2026, 8, 7)
NOW = datetime(2026, 8, 8, 9, 10, tzinfo=UTC)
TEAM_2 = TeamRecord(
    ref="team-2",
    name="法务二部",
    department_name="法务合约中心",
    code="monthly-law-2",
)
TEAM_4 = TeamRecord(
    ref="team-4",
    name="法务四部",
    department_name="法务合约中心",
    code="monthly-law-4",
)
CENTER_REF = "legal-center"


def _member(
    ref: str,
    name: str,
    team: TeamRecord | None,
) -> MemberRecord:
    return MemberRecord(
        ref=ref,
        name=name,
        team_ref=(team.ref if team is not None else CENTER_REF),
        team_name=(team.name if team is not None else "法务合约中心（中心层级）"),
        department_name="法务合约中心",
        team_code=(team.code if team is not None else "legal-center"),
    )


def _obligation(member: MemberRecord) -> SubmissionObligation:
    return SubmissionObligation(
        member_ref=member.ref,
        team_ref=member.team_ref,
        report_date=REPORT_DATE,
        required=True,
        reason="verified_roster",
        deadline_at=datetime(2026, 8, 7, 15, 0, tzinfo=UTC),
        data_complete=True,
        source="test",
    )


def _report(
    member: MemberRecord,
    *,
    status: str,
    with_content: bool,
) -> DailyReportRecord:
    return DailyReportRecord(
        ref=f"report-{member.ref}",
        member_ref=member.ref,
        team_ref=member.team_ref,
        report_date=REPORT_DATE,
        status=status,
        confirmation_type=(
            "auto_submitted_timeout" if status == "completed" else "none"
        ),
        confirmed_by_user=False,
        today_work=((f"{member.name}今日工作",) if with_content else ()),
        tomorrow_plan=((f"{member.name}明日计划",) if with_content else ()),
    )


def _managed_query() -> ManagedDailyQuery:
    ding = _member("ding", "丁益明", TEAM_2)
    xue = _member("xue", "薛旭", TEAM_4)
    zhao = _member("zhao", "赵卫中", None)
    zhu = _member("zhu", "朱佳佳", None)
    return ManagedDailyQuery(
        InMemoryDashboardRepository(
            teams=(TEAM_2, TEAM_4),
            members=(ding, xue, zhao, zhu),
            obligations=tuple(
                _obligation(member)
                for member in (ding, xue, zhao, zhu)
            ),
            reports=(
                _report(ding, status="completed", with_content=True),
                _report(xue, status="completed", with_content=True),
                _report(zhu, status="collecting", with_content=True),
            ),
        )
    )


@pytest.mark.asyncio
async def test_all_scope_missing_query_includes_center_direct_members() -> None:
    result = await _managed_query().execute(
        actor=DashboardActor(tenant_id="tenant", user_id="actor"),
        request=ManagedDailyQueryRequest(
            view="missing_submissions",
            report_date=REPORT_DATE,
        ),
        now=NOW,
    )

    assert result["scope_name"] == "法务合约中心"
    assert [item["name"] for item in result["completed_members"]] == [
        "丁益明",
        "薛旭",
    ]
    assert [item["name"] for item in result["partial_members"]] == [
        "朱佳佳"
    ]
    assert [item["name"] for item in result["not_filled_members"]] == [
        "赵卫中"
    ]
    assert result["partial_members"][0]["team_name"] == "中心直属"
    assert result["not_filled_members"][0]["team_name"] == "中心直属"


@pytest.mark.asyncio
async def test_center_direct_member_report_is_queryable_without_eighth_team() -> None:
    result = await _managed_query().execute(
        actor=DashboardActor(tenant_id="tenant", user_id="actor"),
        request=ManagedDailyQueryRequest(
            view="member_report",
            report_date=REPORT_DATE,
            member_name="朱佳佳",
        ),
        now=NOW,
    )

    assert result["member"] == {
        "name": "朱佳佳",
        "team_name": "中心直属",
        "team_label": "法务合约中心 / 中心直属",
    }


@pytest.mark.asyncio
async def test_member_submission_time_uses_query_timezone() -> None:
    zhu = _member("zhu", "朱佳佳", None)
    report = replace(
        _report(zhu, status="completed", with_content=True),
        submitted_at=datetime(2026, 8, 8, 1, 0, tzinfo=UTC),
    )
    query = ManagedDailyQuery(
        InMemoryDashboardRepository(
            teams=(TEAM_2, TEAM_4),
            members=(zhu,),
            obligations=(_obligation(zhu),),
            reports=(report,),
        )
    )

    result = await query.execute(
        actor=DashboardActor(tenant_id="tenant", user_id="actor"),
        request=ManagedDailyQueryRequest(
            view="member_report",
            report_date=REPORT_DATE,
            member_name="朱佳佳",
        ),
        now=datetime(
            2026,
            8,
            8,
            9,
            10,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )

    assert result["submission"]["submitted_at"] == (
        "2026-08-08T09:00:00+08:00"
    )


def _managed_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="query_managed_daily_reports",
        changed=False,
        target_type="managed_daily_report",
        target_id="query-1",
        safe_user_facts={
            "actual_write": False,
            "managed_daily_query": {
                "query_kind": "missing_submissions",
                "report_date": "2026-08-07",
                "scope_name": "法务合约中心",
                "completed_members": [
                    {"name": "丁益明", "team_name": "法务二部"}
                ],
                "partial_members": [
                    {"name": "朱佳佳", "team_name": "中心直属"}
                ],
                "not_filled_members": [
                    {"name": "赵卫中", "team_name": "中心直属"}
                ],
            },
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


def test_read_receipt_does_not_replace_agent2_model_reply() -> None:
    content, _ = finalize_canary_content(
        "这是Agent2根据可信数据组织的回复。",
        (_managed_receipt(),),
        write_batch_seen=False,
    )

    assert content == "这是Agent2根据可信数据组织的回复。"


def test_managed_daily_reply_validation_checks_facts_not_exact_wording() -> None:
    valid = (
        "2026年8月7日，法务合约中心日报填写情况：\n"
        "已完成 1人：丁益明\n"
        "部分填写 1人：朱佳佳\n"
        "未填写 1人：赵卫中"
    )

    assert validate_managed_daily_reply(valid, (_managed_receipt(),)) == ()
    assert "missing_members:未填写:赵卫中" in validate_managed_daily_reply(
        valid.replace("赵卫中", "其他人"),
        (_managed_receipt(),),
    )
    assert "internal_terms:confirmed missing" in validate_managed_daily_reply(
        valid + "\n赵卫中属于 confirmed missing。",
        (_managed_receipt(),),
    )


def test_managed_daily_reply_accepts_selected_team_leaf_scope() -> None:
    receipt = _managed_receipt()
    safe_facts = dict(receipt.safe_user_facts)
    query = dict(safe_facts["managed_daily_query"])
    query["scope_name"] = "法务合约中心 / 中心直属"
    safe_facts["managed_daily_query"] = query
    selected_team_receipt = receipt.model_copy(
        update={"safe_user_facts": safe_facts}
    )
    content = (
        "2026年8月7日，中心直属日报填写情况：\n"
        "已完成 1人：丁益明\n"
        "部分填写 1人：朱佳佳\n"
        "未填写 1人：赵卫中"
    )

    assert validate_managed_daily_reply(
        content,
        (selected_team_receipt,),
    ) == ()


def test_managed_daily_prompt_requires_stable_plain_text_layout() -> None:
    prompt = canary_system_prompt()

    assert "Managed daily-report reply contract" in prompt
    assert "已完成、部分填写、未填写" in prompt
    assert "`中心直属` is a valid team scope" in prompt
    assert "Never let server-side formatting replace your business reply" in prompt


def test_managed_daily_tool_schema_explains_explicit_team_scope() -> None:
    schema = QueryManagedDailyReportsArgs.model_json_schema()
    team_description = schema["properties"]["team_name"]["description"]

    assert "MUST be supplied" in team_description
    assert "中心直属" in team_description


def test_only_structured_model_acknowledgement_counts_as_empty_problem() -> None:
    legacy_text_only = type(
        "Report",
        (),
        {"section_status": {}, "problems": ["暂无问题"]},
    )()
    model_acknowledged = type(
        "Report",
        (),
        {
            "section_status": {"problems_acknowledged_empty": True},
            "problems": [],
        },
    )()

    assert problems_acknowledged_empty(legacy_text_only) is False
    assert problems_acknowledged_empty(model_acknowledged) is True


@pytest.mark.asyncio
async def test_canary_managed_query_initializes_reply_retry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("11111111-1111-1111-1111-111111111111"),
            conversation_id="conversation",
            source_message_id="message",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"query_managed_daily_reports"}),
        gate_decisions={"query_managed_daily_reports": True},
    )

    class RuntimeSession:
        mode = ExecutionMode.CANARY_EXECUTE

        async def execute(self, calls):
            assert len(calls) == 1
            assert calls[0].tool_name == "query_managed_daily_reports"
            return ProductionRuntimeResult(
                status="success",
                receipts=(_managed_receipt(),),
                handler_call_count=1,
            )

    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    terminal_reply = (
        "2026年8月7日，法务合约中心日报填写情况：\n"
        "已完成 1人：丁益明\n"
        "部分填写 1人：朱佳佳\n"
        "未填写 1人：赵卫中"
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "query_managed_daily_reports",
                                "arguments": json.dumps(
                                    {
                                        "view": "missing_submissions",
                                        "report_date_expression": "昨天",
                                        "proposed_report_date": "2026-08-07",
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
                message={
                    "role": "assistant",
                    "content": terminal_reply,
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "decision": "keep",
                            "classification": "ordinary_reply",
                            "reviewed_reply_sha256": hashlib.sha256(
                                terminal_reply.encode("utf-8")
                            ).hexdigest(),
                            "pending_reference": None,
                            "replacement_reply": None,
                        }
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)
    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="昨天谁填了日报？",
        context=context,
        runtime_session=RuntimeSession(),
    )

    assert result.iterations == 3
    assert result.receipts == (_managed_receipt(),)
    assert "丁益明" in result.final_content
