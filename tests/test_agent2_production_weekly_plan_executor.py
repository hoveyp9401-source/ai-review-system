from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import TrustedContext, TrustedPrincipal
from app.agent2.tool_calling.contracts import (
    ApplyNextWeeklyPlanArgs,
    QueryNextWeeklyPlanArgs,
    ReceiptStatus,
    SubmitNextWeeklyPlanArgs,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.production_daily_executor import ProductionExecutionError
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.validation import BoundCall, NativeToolCall
from app.agent2.weekly_plan_context import build_trusted_weekly_plan_context
from app.agent2.weekly_plan_domain import (
    add_weekly_plan_suggestion,
    create_weekly_plan,
    create_weekly_plan_batch,
    execute_weekly_plan_batch,
    execute_weekly_plan_command,
)
from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.agent2.weekly_plan_suggestions import (
    TrustedSourceKind,
    build_trusted_evidence,
)

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TENANT_ID = "tenant-1"
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
WEEK_START = date(2026, 8, 17)


AUTHORITY_MEMBER = WeeklyPlanRosterMember(
    user_id=str(USER_ID),
    display_name="搴炴旦",
    department_id="department-authoritative",
    team_id="team-authoritative",
)
SECOND_AUTHORITY_MEMBER = WeeklyPlanRosterMember(
    user_id="33333333-3333-4333-8333-333333333333",
    display_name="测试用户乙",
    department_id="department-authoritative",
    team_id="team-authoritative",
)


class _FakeWeeklyPlanStore:
    def __init__(self, plan=None, *, batch=None):
        self.plan = deepcopy(plan)
        self.batch = deepcopy(batch)
        self.writes: list[str] = []

    async def load_plan_by_owner_week(
        self,
        *,
        tenant_id,
        owner_user_id,
        target_week_start,
        for_update=False,
    ):
        del for_update
        if self.plan is None:
            return None
        if (
            self.plan.tenant_id != tenant_id
            or self.plan.owner_user_id != owner_user_id
            or self.plan.target_week_start != target_week_start
        ):
            return None
        return deepcopy(self.plan)

    async def open_or_load_batch(self, batch):
        self.writes.append("batch")
        if self.batch is None:
            self.batch = deepcopy(batch)
        elif self.batch != batch:
            raise ValueError("weekly_plan_frozen_roster_mismatch")
        return deepcopy(self.batch)

    async def save_batch(self, batch):
        await self.open_or_load_batch(batch)

    async def create_plan(self, plan):
        self.writes.append("plan")
        self.plan = deepcopy(plan)
        return deepcopy(plan)

    async def execute_batch(self, commands, *, executed_at):
        self.writes.append("batch-commands")
        execution = execute_weekly_plan_batch(
            commands,
            plan=self.plan,
            executed_at=executed_at,
        )
        if execution.receipt.actual_write:
            self.plan = deepcopy(execution.after)
        return (deepcopy(execution),)

    async def execute(self, command, *, executed_at):
        self.writes.append("command")
        execution = execute_weekly_plan_command(
            command,
            plan=self.plan,
            executed_at=executed_at,
        )
        if execution.receipt.actual_write:
            self.plan = deepcopy(execution.after)
        return execution


class _ConflictingCreateStore(_FakeWeeklyPlanStore):
    async def create_plan(self, plan):
        self.writes.append("plan")
        conflicted = replace(plan, version=1)
        self.plan = conflicted
        return deepcopy(conflicted)


def _domain_plan(*, week_start=WEEK_START):
    batch = create_weekly_plan_batch(
        tenant_id=TENANT_ID,
        target_week_start=week_start,
        roster=(
            WeeklyPlanRosterMember(
                user_id=str(USER_ID),
                display_name="测试用户甲",
                team_id="team-1",
                team_name="综合管理部",
            ),
        ),
        created_at=NOW,
    )
    return create_weekly_plan(
        batch=batch,
        owner_user_id=str(USER_ID),
        created_at=NOW,
    )


def _context(plan=None, *, now=NOW):
    plan = plan or _domain_plan()
    weekly = build_trusted_weekly_plan_context(
        plan=plan,
        authenticated_tenant_id=TENANT_ID,
        authenticated_owner_user_id=str(USER_ID),
        target_week_start=WEEK_START,
    )
    return TrustedContext(
        now=now,
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="private-conversation-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            display_name="测试用户甲",
            conversation_kind="direct",
        ),
        weekly_plan=weekly,
    )


def _user():
    return SimpleNamespace(
        id=USER_ID,
        name="测试用户甲",
        team_id=UUID("22222222-2222-4222-8222-222222222222"),
    )


def _request(tool_name, arguments, *, weekly_plan=None):
    call = NativeToolCall(
        tool_call_id=f"call-{tool_name}",
        tool_name=tool_name,
        arguments=arguments.model_dump(mode="json"),
    )
    bound = BoundCall(
        call=call,
        arguments=call.arguments,
        report=None,
        target_item_ids=(),
        source_report=None,
        date_facts={},
        weekly_plan=weekly_plan,
    )
    request = ProductionHandlerRequest(
        tool_call_id=call.tool_call_id,
        tool_name=tool_name,
        arguments=arguments,
        executor=object(),
        memory_executor=object(),
    )
    return request, {call.tool_call_id: bound}


def _executor(
    *,
    store,
    context,
    bound_calls,
    source,
    authoritative_member=AUTHORITY_MEMBER,
    session=None,
    settings=None,
    now_provider=None,
):
    from app.agent2.tool_calling.production_weekly_plan_executor import (
        ProductionWeeklyPlanExecutor,
    )

    trusted_bound_calls = {
        call_id: (
            bound
            if bound.weekly_plan is not None
            else replace(bound, weekly_plan=context.weekly_plan)
        )
        for call_id, bound in bound_calls.items()
    }
    return ProductionWeeklyPlanExecutor(
        session=session,
        user=_user(),
        context=context,
        bound_calls=trusted_bound_calls,
        current_turn_source=source,
        store=store,
        settings=settings,
        authoritative_roster_member=authoritative_member,
        now_provider=now_provider or (lambda: context.now),
    )


@pytest.mark.asyncio
async def test_query_absent_plan_returns_exact_virtual_six_day_preview_with_zero_writes():
    context = _context()
    store = _FakeWeeklyPlanStore()
    request, bound_calls = _request(
        "query_next_weekly_plan",
        QueryNextWeeklyPlanArgs(),
    )
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("看看我的下周计划",)),
    )

    outcome = await executor.query_next_weekly_plan(request)

    assert store.writes == []
    assert outcome.target_type == "weekly_plan"
    assert outcome.safe_user_facts["actual_write"] is False
    preview = outcome.safe_user_facts["formal_plan"]
    assert preview["target_week_start"] == "2026-08-17"
    assert [day["plan_date"] for day in preview["days"]] == [
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-22",
    ]
    assert all(day["state"] == "unfilled" for day in preview["days"])
    assert outcome.safe_user_facts["suggestion_zone"] == []
    assert "weekly_receipt_ids" not in outcome.safe_user_facts


@pytest.mark.asyncio
async def test_apply_executes_the_second_plan_selected_by_the_bound_call():
    first_plan = _domain_plan()
    second_plan = _domain_plan(week_start=WEEK_START + timedelta(days=7))
    first = build_trusted_weekly_plan_context(
        plan=first_plan,
        authenticated_tenant_id=TENANT_ID,
        authenticated_owner_user_id=str(USER_ID),
        target_week_start=first_plan.target_week_start,
    )
    second = build_trusted_weekly_plan_context(
        plan=second_plan,
        authenticated_tenant_id=TENANT_ID,
        authenticated_owner_user_id=str(USER_ID),
        target_week_start=second_plan.target_week_start,
    )
    context = _context(first_plan).model_copy(
        update={"weekly_plan": first, "weekly_plans": (first, second)}
    )
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": second.plan_id,
            "expected_version": second.version,
            "operations": [
                {
                    "operation_id": "second-add",
                    "operation": "add",
                    "plan_date": "2026-08-24",
                    "content": "整理第二周材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "下周一整理第二周材料",
                    },
                }
            ],
        }
    )
    request, bound_calls = _request(
        "apply_next_weekly_plan",
        arguments,
        weekly_plan=second,
    )
    store = _FakeWeeklyPlanStore(second_plan)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("下周一整理第二周材料",)),
    )

    outcome = await executor.apply_next_weekly_plan(request)

    assert outcome.target_id == second.plan_id
    assert outcome.safe_user_facts["formal_plan"]["target_week_start"] == "2026-08-24"
    assert store.plan.days[0].items[0].original_text == "整理第二周材料"


@pytest.mark.asyncio
async def test_query_reads_the_second_plan_selected_by_the_bound_call():
    first_plan = _domain_plan()
    second_plan = _domain_plan(week_start=WEEK_START + timedelta(days=7))
    first = build_trusted_weekly_plan_context(
        plan=first_plan,
        authenticated_tenant_id=TENANT_ID,
        authenticated_owner_user_id=str(USER_ID),
        target_week_start=first_plan.target_week_start,
    )
    second = build_trusted_weekly_plan_context(
        plan=second_plan,
        authenticated_tenant_id=TENANT_ID,
        authenticated_owner_user_id=str(USER_ID),
        target_week_start=second_plan.target_week_start,
    )
    context = _context(first_plan).model_copy(
        update={"weekly_plan": first, "weekly_plans": (first, second)}
    )
    request, bound_calls = _request(
        "query_next_weekly_plan",
        QueryNextWeeklyPlanArgs(plan_id=second.plan_id),
        weekly_plan=second,
    )
    executor = _executor(
        store=_FakeWeeklyPlanStore(second_plan),
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("查看 8 月 24 日开始的工作计划",)),
    )

    outcome = await executor.query_next_weekly_plan(request)

    assert outcome.target_id == second.plan_id
    assert outcome.safe_user_facts["formal_plan"]["target_week_start"] == "2026-08-24"


@pytest.mark.asyncio
async def test_executor_fails_closed_when_bound_weekly_target_is_missing():
    context = _context()
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "missing-bound-target",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "周一整理材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "周一整理材料",
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    from app.agent2.tool_calling.production_weekly_plan_executor import (
        ProductionWeeklyPlanExecutor,
    )

    executor = ProductionWeeklyPlanExecutor(
        session=None,
        user=_user(),
        context=context,
        bound_calls=bound_calls,
        current_turn_source=CurrentTurnSource(("周一整理材料",)),
        store=_FakeWeeklyPlanStore(_domain_plan()),
    )

    with pytest.raises(
        ProductionExecutionError,
        match="BOUND_WEEKLY_PLAN_REQUIRED",
    ):
        await executor.apply_next_weekly_plan(request)


@pytest.mark.asyncio
async def test_live_plan_keeps_suggestion_separate_until_current_user_accepts_it():
    plan = _domain_plan()
    evidence_text = "本周提到继续优化合同评审能力"
    evidence = build_trusted_evidence(
        owner_user_id=str(USER_ID),
        source_kind=TrustedSourceKind.USER_ORIGINAL_MESSAGE,
        source_ref="daily-original-message-1",
        source_version="1",
        evidence_text=evidence_text,
    )
    plan = add_weekly_plan_suggestion(
        plan=plan,
        evidence=evidence,
        matter_excerpt="继续优化合同评审能力",
        created_at=NOW,
        expires_at=NOW + timedelta(days=10),
    )
    context = _context(plan)
    store = _FakeWeeklyPlanStore(plan)
    query_request, query_bound = _request(
        "query_next_weekly_plan",
        QueryNextWeeklyPlanArgs(),
    )
    executor = _executor(
        store=store,
        context=context,
        bound_calls=query_bound,
        source=CurrentTurnSource(("看看我的下周计划",)),
    )

    queried = await executor.query_next_weekly_plan(query_request)

    assert queried.safe_user_facts["formal_plan"]["days"][0]["items"] == []
    suggestion = queried.safe_user_facts["suggestion_zone"][0]
    assert suggestion["evidence_excerpt"] == "继续优化合同评审能力"
    assert suggestion["is_formal_plan_item"] is False
    assert "暂时没找到后续记录" in suggestion["prompt"]
    assert store.writes == []

    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": plan.plan_id,
            "expected_version": plan.version,
            "operations": [
                {
                    "operation_id": "accept-1",
                    "operation": "accept_suggestion",
                    "suggestion_id": plan.suggestions[0].suggestion_id,
                    "plan_date": "2026-08-17",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "把这个建议放到周一",
                    },
                }
            ],
        }
    )
    apply_request, apply_bound = _request(
        "apply_next_weekly_plan",
        arguments,
    )
    executor = _executor(
        store=store,
        context=context,
        bound_calls=apply_bound,
        source=CurrentTurnSource(("把这个建议放到周一",)),
    )

    accepted = await executor.apply_next_weekly_plan(apply_request)

    monday = accepted.safe_user_facts["formal_plan"]["days"][0]
    assert monday["items"][0]["content"] == "继续优化合同评审能力"
    assert accepted.safe_user_facts["suggestion_zone"] == []
    assert store.plan.suggestions[0].status.value == "accepted"


@pytest.mark.asyncio
async def test_first_apply_preflights_then_atomically_creates_and_applies_six_days():
    context = _context()
    store = _FakeWeeklyPlanStore()
    message = "周一整理材料；周二开庭；周三优化技能；周四跟进项目；周五整理进展；周六暂无安排"
    operations = [
        {
            "operation_id": f"add-{offset}",
            "operation": "add",
            "plan_date": (WEEK_START.replace(day=17 + offset)).isoformat(),
            "content": content,
            "source_evidence": {
                "source_message_index": 1,
                "exact_clause_quote": message.split("；")[offset],
            },
        }
        for offset, content in enumerate(
            ("整理材料", "开庭", "优化技能", "跟进项目", "整理进展")
        )
    ]
    operations.append(
        {
            "operation_id": "empty-5",
            "operation": "set_day_empty",
            "plan_date": "2026-08-22",
            "source_evidence": {
                "source_message_index": 1,
                "exact_clause_quote": "周六暂无安排",
            },
        }
    )
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": operations,
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource((message,)),
    )

    outcome = await executor.apply_next_weekly_plan(request)

    assert store.writes == ["batch", "plan", "batch-commands"]
    assert outcome.safe_user_facts["actual_write"] is True
    assert outcome.before_version == 0
    assert outcome.after_version == 1
    preview = outcome.safe_user_facts["formal_plan"]
    assert preview["status"] == "pending_confirmation"
    assert [day["state"] for day in preview["days"]] == [
        "planned",
        "planned",
        "planned",
        "planned",
        "planned",
        "explicitly_empty",
    ]
    assert [
        day["items"][0]["content"] for day in preview["days"][:5]
    ] == ["整理材料", "开庭", "优化技能", "跟进项目", "整理进展"]


@pytest.mark.asyncio
async def test_first_write_freezes_authoritative_identity_roster_for_friday_replay():
    context = _context()
    store = _FakeWeeklyPlanStore()
    message = "next Monday prepare materials"
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "early-authoritative-roster",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "prepare materials",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": message,
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource((message,)),
    )

    await executor.apply_next_weekly_plan(request)

    assert store.batch.roster == (AUTHORITY_MEMBER,)
    replay = create_weekly_plan_batch(
        tenant_id=TENANT_ID,
        target_week_start=WEEK_START,
        roster=(AUTHORITY_MEMBER,),
        created_at=NOW,
    )
    assert await store.open_or_load_batch(replay) == store.batch


@pytest.mark.asyncio
async def test_production_first_write_loads_both_configured_identity_bindings():
    context = _context()
    store = _FakeWeeklyPlanStore()
    message = "next Monday prepare materials"
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "configured-two-user-roster",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "prepare materials",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": message,
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)

    class _Bindings:
        def all(self):
            return (
                SimpleNamespace(
                    user_id=SECOND_AUTHORITY_MEMBER.user_id,
                    display_name=SECOND_AUTHORITY_MEMBER.display_name,
                    department_id=SECOND_AUTHORITY_MEMBER.department_id,
                    team_id=SECOND_AUTHORITY_MEMBER.team_id,
                ),
                SimpleNamespace(
                    user_id=AUTHORITY_MEMBER.user_id,
                    display_name=AUTHORITY_MEMBER.display_name,
                    department_id=AUTHORITY_MEMBER.department_id,
                    team_id=AUTHORITY_MEMBER.team_id,
                ),
            )

    class _Session:
        async def scalars(self, _statement):
            return _Bindings()

    settings = SimpleNamespace(
        agent2_weekly_plan_tenant_allowlist=TENANT_ID,
        agent2_weekly_plan_user_allowlist=(
            f"{SECOND_AUTHORITY_MEMBER.user_id},{AUTHORITY_MEMBER.user_id}"
        ),
    )
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource((message,)),
        authoritative_member=None,
        session=_Session(),
        settings=settings,
    )

    await executor.apply_next_weekly_plan(request)

    assert store.batch.roster == tuple(
        sorted(
            (AUTHORITY_MEMBER, SECOND_AUTHORITY_MEMBER),
            key=lambda member: member.user_id,
        )
    )
    replay = create_weekly_plan_batch(
        tenant_id=TENANT_ID,
        target_week_start=WEEK_START,
        roster=store.batch.roster,
        created_at=NOW,
    )
    assert await store.open_or_load_batch(replay) == store.batch


@pytest.mark.asyncio
async def test_production_first_write_loads_the_full_74_user_roster():
    context = _context()
    store = _FakeWeeklyPlanStore()
    message = "next Monday prepare materials"
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "configured-full-roster",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "prepare materials",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": message,
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    user_ids = (str(USER_ID),) + tuple(
        f"full-roster-user-{index:03d}" for index in range(1, 74)
    )
    bindings = tuple(
        SimpleNamespace(
            user_id=user_id,
            display_name=f"测试成员{index:03d}",
            department_id="department-authoritative",
            team_id="team-authoritative",
        )
        for index, user_id in enumerate(user_ids)
    )

    class _Bindings:
        def all(self):
            return tuple(reversed(bindings))

    class _Session:
        async def scalars(self, _statement):
            return _Bindings()

    settings = SimpleNamespace(
        agent2_weekly_plan_tenant_allowlist=TENANT_ID,
        agent2_weekly_plan_user_allowlist=",".join(reversed(user_ids)),
    )
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource((message,)),
        authoritative_member=None,
        session=_Session(),
        settings=settings,
    )

    outcome = await executor.apply_next_weekly_plan(request)

    assert outcome.safe_user_facts["actual_write"] is True
    assert len(store.batch.roster) == 74
    assert tuple(member.user_id for member in store.batch.roster) == tuple(
        sorted(user_ids)
    )
    assert store.plan.owner_user_id == str(USER_ID)


@pytest.mark.asyncio
async def test_undated_next_week_statement_is_persisted_only_in_suggestion_zone():
    context = _context()
    store = _FakeWeeklyPlanStore()
    content = "下周继续整理合同评审规则"
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "suggest-1",
                    "operation": "capture_suggestion",
                    "content": content,
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource((content,)),
    )

    outcome = await executor.apply_next_weekly_plan(request)

    assert all(
        day["items"] == []
        for day in outcome.safe_user_facts["formal_plan"]["days"]
    )
    suggestion = outcome.safe_user_facts["suggestion_zone"][0]
    assert suggestion["evidence_excerpt"] == content
    assert suggestion["is_formal_plan_item"] is False
    assert store.plan.suggestions[0].source_ref == "message-1"


@pytest.mark.asyncio
async def test_invalid_first_apply_leaves_no_empty_batch_or_plan():
    context = _context()
    store = _FakeWeeklyPlanStore()
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "bad-date",
                    "operation": "add",
                    "plan_date": "2026-08-23",
                    "content": "整理材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "周日整理材料",
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("周日整理材料",)),
    )

    with pytest.raises(ProductionExecutionError, match="WEEKLY_PLAN_DATE_OUT_OF_SCOPE"):
        await executor.apply_next_weekly_plan(request)

    assert store.writes == []
    assert store.plan is None


@pytest.mark.asyncio
async def test_first_apply_stops_if_concurrent_create_is_not_the_expected_empty_plan():
    context = _context()
    store = _ConflictingCreateStore()
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "add-1",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "整理材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "周一整理材料",
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("周一整理材料",)),
    )

    with pytest.raises(ProductionExecutionError, match="WEEKLY_PLAN_VERSION_MISMATCH"):
        await executor.apply_next_weekly_plan(request)

    assert store.writes == ["batch", "plan"]
    assert "batch-commands" not in store.writes


@pytest.mark.asyncio
async def test_apply_rejects_cross_owner_or_stale_context_before_writing():
    context = _context()
    store = _FakeWeeklyPlanStore()
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": "33333333-3333-4333-8333-333333333333",
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "add-1",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "整理材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "周一整理材料",
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("周一整理材料",)),
    )

    with pytest.raises(ProductionExecutionError, match="WEEKLY_PLAN_BINDING_MISMATCH"):
        await executor.apply_next_weekly_plan(request)

    assert store.writes == []


@pytest.mark.asyncio
async def test_current_week_plan_write_closes_after_monday_late_fill_window():
    tuesday = datetime(2026, 8, 18, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    plan = _domain_plan()
    context = _context(plan, now=tuesday)
    store = _FakeWeeklyPlanStore(plan)
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "closed-current-week",
                    "operation": "add",
                    "plan_date": "2026-08-19",
                    "content": "整理材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "本周三整理材料",
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("本周三整理材料",)),
    )

    with pytest.raises(
        ProductionExecutionError,
        match="WEEKLY_PLAN_LATE_FILL_WINDOW_CLOSED",
    ):
        await executor.apply_next_weekly_plan(request)

    assert store.writes == []


@pytest.mark.asyncio
async def test_write_window_rechecks_fresh_beijing_server_time_at_execution():
    monday_context_time = datetime(
        2026, 8, 17, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    tuesday_execution_time = datetime(
        2026, 8, 18, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    plan = _domain_plan()
    context = _context(plan, now=monday_context_time)
    store = _FakeWeeklyPlanStore(plan)
    message = "this Wednesday prepare materials"
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "cross-midnight-closed",
                    "operation": "add",
                    "plan_date": "2026-08-19",
                    "content": "prepare materials",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": message,
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource((message,)),
        now_provider=lambda: tuesday_execution_time,
    )

    with pytest.raises(
        ProductionExecutionError,
        match="WEEKLY_PLAN_LATE_FILL_WINDOW_CLOSED",
    ):
        await executor.apply_next_weekly_plan(request)

    assert store.writes == []


@pytest.mark.asyncio
async def test_query_rejects_malformed_live_week_even_when_owner_and_id_match():
    plan = _domain_plan()
    malformed = replace(plan, days=plan.days[:5])
    context = _context(plan)
    store = _FakeWeeklyPlanStore(malformed)
    request, bound_calls = _request(
        "query_next_weekly_plan",
        QueryNextWeeklyPlanArgs(),
    )
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("看看我的下周计划",)),
    )

    with pytest.raises(ProductionExecutionError, match="WEEKLY_PLAN_WEEK_SHAPE_INVALID"):
        await executor.query_next_weekly_plan(request)

    assert store.writes == []


@pytest.mark.asyncio
async def test_submit_requires_current_turn_evidence_and_all_six_days_resolved():
    plan = _domain_plan()
    context = _context(plan)
    store = _FakeWeeklyPlanStore(plan)
    arguments = SubmitNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": plan.plan_id,
            "expected_version": 0,
            "confirmation_evidence": {"source_message_index": 1},
        }
    )
    request, bound_calls = _request("submit_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("确认提交",)),
    )

    outcome = await executor.submit_next_weekly_plan(request)

    assert store.writes == []
    assert outcome.status_if_unchanged == ReceiptStatus.BLOCKED
    assert outcome.error_code == "WEEKLY_PLAN_UNRESOLVED_DAYS"
    assert outcome.safe_user_facts["formal_plan"]["unresolved_dates"] == [
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-22",
    ]


@pytest.mark.asyncio
async def test_submit_of_resolved_plan_preserves_exact_preview_and_writes_no_daily_report():
    plan = _domain_plan()
    # Resolve each day using domain commands before building trusted context.
    for offset, day in enumerate(plan.days):
        from app.agent2.weekly_plan_models import WeeklyPlanCommand

        typed = WeeklyPlanCommand(
            command_id=f"seed-{offset}",
            command_type="set_day_empty",
            tenant_id=TENANT_ID,
            actor_user_id=str(USER_ID),
            plan_id=plan.plan_id,
            expected_version=plan.version,
            idempotency_key=f"seed-{offset}",
            source_message_id="seed-message",
            patch={"plan_date": day.plan_date.isoformat()},
        )
        plan = execute_weekly_plan_command(
            typed,
            plan=plan,
            executed_at=NOW,
        ).after
    context = _context(plan)
    store = _FakeWeeklyPlanStore(plan)
    arguments = SubmitNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": plan.plan_id,
            "expected_version": plan.version,
            "confirmation_evidence": {"source_message_index": 1},
        }
    )
    request, bound_calls = _request("submit_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("以上下周计划确认提交",)),
    )

    outcome = await executor.submit_next_weekly_plan(request)

    assert store.writes == ["command"]
    assert outcome.target_type == "weekly_plan"
    assert outcome.before_report is None
    assert outcome.after_report is None
    assert outcome.safe_user_facts["formal_plan"]["status"] == "submitted"
    assert all(
        day["state"] == "explicitly_empty"
        for day in outcome.safe_user_facts["formal_plan"]["days"]
    )
    assert outcome.safe_user_facts["suggestion_zone"] == []


@pytest.mark.asyncio
async def test_repeated_submit_of_already_submitted_plan_is_a_zero_write_no_op():
    plan = _domain_plan()
    from app.agent2.weekly_plan_models import WeeklyPlanCommand

    for offset, day in enumerate(plan.days):
        plan = execute_weekly_plan_command(
            WeeklyPlanCommand(
                command_id=f"seed-empty-{offset}",
                command_type="set_day_empty",
                tenant_id=TENANT_ID,
                actor_user_id=str(USER_ID),
                plan_id=plan.plan_id,
                expected_version=plan.version,
                idempotency_key=f"seed-empty-{offset}",
                source_message_id="seed-message",
                patch={"plan_date": day.plan_date.isoformat()},
            ),
            plan=plan,
            executed_at=NOW,
        ).after
    plan = execute_weekly_plan_command(
        WeeklyPlanCommand(
            command_id="seed-submit",
            command_type="submit_plan",
            tenant_id=TENANT_ID,
            actor_user_id=str(USER_ID),
            plan_id=plan.plan_id,
            expected_version=plan.version,
            idempotency_key="seed-submit",
            source_message_id="seed-message",
        ),
        plan=plan,
        executed_at=NOW,
    ).after
    context = _context(plan)
    store = _FakeWeeklyPlanStore(plan)
    arguments = SubmitNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": plan.plan_id,
            "expected_version": plan.version,
            "confirmation_evidence": {"source_message_index": 1},
        }
    )
    request, bound_calls = _request("submit_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=CurrentTurnSource(("确认提交",)),
    )

    outcome = await executor.submit_next_weekly_plan(request)

    assert store.writes == []
    assert outcome.safe_user_facts["actual_write"] is False
    assert outcome.before_version == plan.version
    assert outcome.after_version == plan.version
    assert outcome.safe_user_facts["formal_plan"]["status"] == "submitted"


@pytest.mark.asyncio
async def test_weekly_writes_fail_closed_without_server_current_turn_source():
    context = _context()
    store = _FakeWeeklyPlanStore()
    arguments = ApplyNextWeeklyPlanArgs.model_validate(
        {
            "plan_id": context.weekly_plan.plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "add-1",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "整理材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "周一整理材料",
                    },
                }
            ],
        }
    )
    request, bound_calls = _request("apply_next_weekly_plan", arguments)
    executor = _executor(
        store=store,
        context=context,
        bound_calls=bound_calls,
        source=None,
    )

    with pytest.raises(ProductionExecutionError, match="CURRENT_TURN_SOURCE_REQUIRED"):
        await executor.apply_next_weekly_plan(request)
    assert store.writes == []
