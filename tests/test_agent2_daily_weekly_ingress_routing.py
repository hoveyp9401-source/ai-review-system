from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
    canary_system_prompt,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionExecutionCapability,
    ProductionRuntimeResult,
)
from app.agent2.tool_calling.production_runtime import (
    ProductionRuntime as RealProductionRuntime,
)
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.agent2.weekly_plan_domain import _stable_id

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 14, 18, 0, tzinfo=SHANGHAI)
TENANT_ID = "tenant-a"
ALLOWED_USER_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_USER_ID = UUID("10000000-0000-4000-8000-000000000002")
TARGET_WEEK = date(2026, 8, 17)


class _EmptyResult:
    """Enough of SQLAlchemy's read-result surface for an empty trusted store."""

    def scalars(self):
        return self

    def mappings(self):
        return self

    def all(self):
        return []

    def one_or_none(self):
        return None

    def scalar_one_or_none(self):
        return None


class _Savepoint:
    def __init__(self, session: _IngressSession) -> None:
        self._session = session
        self._before = deepcopy(session.working)
        self.is_active = True

    async def commit(self) -> None:
        self.is_active = False
        self._session.outer_commit_count += 1

    async def rollback(self) -> None:
        self.is_active = False
        self._session.working = deepcopy(self._before)
        self._session.outer_rollback_count += 1


class _IngressSession:
    """Read-empty SQL boundary plus an observable in-memory write ledger."""

    def __init__(self) -> None:
        self.working = {"daily": [], "weekly": []}
        self.committed = deepcopy(self.working)
        self.outer_commit_count = 0
        self.outer_rollback_count = 0

    async def scalar(self, _statement):
        return None

    async def scalars(self, _statement):
        return _EmptyResult()

    async def execute(self, _statement, _parameters=None):
        return _EmptyResult()

    async def begin_nested(self):
        return _Savepoint(self)


class _HttpResponse:
    def __init__(self, message: dict, sequence: int) -> None:
        self._message = message
        self._sequence = sequence

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "id": f"scripted-{self._sequence}",
            "model": CANARY_MODEL_NAME,
            "created": 1,
            "choices": [
                {
                    "finish_reason": (
                        "tool_calls" if self._message.get("tool_calls") else "stop"
                    ),
                    "message": self._message,
                }
            ],
            "usage": {},
        }


class _ScriptedHttpClient:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = iter(messages)
        self.calls: list[dict] = []

    async def post(self, _endpoint, *, json, timeout):
        del timeout
        self.calls.append(json)
        return _HttpResponse(next(self._messages), len(self.calls))


class _PendingLedgerRuntime:
    """Keep the real capability and binder, replacing only production writers."""

    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self, real_session, ledger: _IngressSession) -> None:
        self._real = real_session
        self._ledger = ledger
        self._pending: tuple[ToolReceipt, ...] | None = None
        self._pending_before: dict | None = None

    async def execute(
        self,
        calls,
        *,
        commit_to_outer_transaction: bool = True,
        defer_finalization: bool = False,
    ) -> ProductionRuntimeResult:
        del commit_to_outer_transaction
        self._real._binder.begin_batch()
        bound = []
        failures = []
        for call in calls:
            item, failure = await self._real._binder.bind(call)
            if failure is not None:
                failures.append(
                    failure.model_copy(
                        update={
                            "execution_mode": ExecutionMode.CANARY_EXECUTE,
                            "safe_user_facts": {
                                **failure.safe_user_facts,
                                "actual_write": False,
                                "execution_mode": "canary_execute",
                            },
                        }
                    )
                )
            else:
                bound.append(item)
        if failures:
            receipts = tuple(
                failures
                + [
                    ToolReceipt(
                        status=ReceiptStatus.BLOCKED,
                        tool_name=item.call.tool_name,
                        changed=False,
                        error_code="ATOMIC_GROUP_PREVALIDATION_FAILED",
                        safe_user_facts={"actual_write": False},
                        execution_mode=ExecutionMode.CANARY_EXECUTE,
                    )
                    for item in bound
                ]
            )
            return ProductionRuntimeResult(
                status="blocked",
                receipts=receipts,
                error_code=receipts[0].error_code,
            )

        before = deepcopy(self._ledger.working)
        receipts: list[ToolReceipt] = []
        for item in bound:
            call = item.call
            if call.tool_name == "add_daily_items":
                contents = [value["content"] for value in call.arguments["items"]]
                self._ledger.working["daily"].extend(contents)
                target_type = "daily_report"
                target_id = f"daily:{NOW.date().isoformat()}"
            elif call.tool_name == "apply_next_weekly_plan":
                contents = [
                    value.get("content", value["operation"])
                    for value in call.arguments["operations"]
                ]
                self._ledger.working["weekly"].extend(contents)
                target_type = "weekly_plan"
                target_id = str(call.arguments["plan_id"])
            else:  # These entrance tests intentionally exercise only two write domains.
                raise AssertionError(f"unexpected scripted tool: {call.tool_name}")
            receipts.append(
                ToolReceipt(
                    status=ReceiptStatus.SUCCESS,
                    tool_name=call.tool_name,
                    changed=True,
                    target_type=target_type,
                    target_id=target_id,
                    before_version=0,
                    after_version=1,
                    safe_user_facts={"actual_write": True},
                    execution_mode=ExecutionMode.CANARY_EXECUTE,
                )
            )
        receipt_tuple = tuple(receipts)
        committed = ProductionRuntimeResult(
            status="success",
            receipts=receipt_tuple,
            transaction_opened=True,
            committed_to_outer_transaction=True,
            business_write_count=len(receipt_tuple),
        )
        if defer_finalization:
            self._pending = receipt_tuple
            self._pending_before = before
            return ProductionRuntimeResult(
                status="success",
                receipts=receipt_tuple,
                transaction_opened=True,
                transaction_pending=True,
            )
        self._ledger.committed = deepcopy(self._ledger.working)
        return committed

    async def commit_pending(self) -> ProductionRuntimeResult:
        assert self._pending is not None
        receipts = self._pending
        self._pending = None
        self._pending_before = None
        self._ledger.committed = deepcopy(self._ledger.working)
        return ProductionRuntimeResult(
            status="success",
            receipts=receipts,
            transaction_opened=True,
            committed_to_outer_transaction=True,
            business_write_count=len(receipts),
        )

    async def rollback_pending(self) -> None:
        if self._pending_before is not None:
            self._ledger.working = self._pending_before
        self._pending = None
        self._pending_before = None


class _RuntimeFactory:
    def open_session(self, **kwargs):
        real_session = RealProductionRuntime().open_session(**kwargs)
        return _PendingLedgerRuntime(real_session, kwargs["session"])


def _settings(
    *,
    allow_user_id: UUID = ALLOWED_USER_ID,
    weekly_plan_enabled: bool = True,
    weekly_plan_write_enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        timezone="Asia/Shanghai",
        llm_base_url="https://example.invalid",
        agent2_weekly_plan_enabled=weekly_plan_enabled,
        agent2_weekly_plan_write_enabled=weekly_plan_write_enabled,
        agent2_weekly_plan_send_enabled=False,
        agent2_weekly_plan_tenant_allowlist=TENANT_ID,
        agent2_weekly_plan_user_allowlist=str(allow_user_id),
        agent2_weekly_plan_send_user_allowlist="",
        legal_daily_dashboard_enabled=False,
        agent2_cross_user_daily_read_enabled=False,
        agent2_performance_tool_enabled=False,
        legal_ops_data_intake_enabled=False,
        agent2_performance_knowledge_enabled=False,
    )


def _plan_id(
    user_id: UUID = ALLOWED_USER_ID,
    target_week: date = TARGET_WEEK,
) -> str:
    return _stable_id(
        "weekly-plan", TENANT_ID, str(user_id), target_week.isoformat()
    )


def _daily_call(
    call_id: str = "daily",
    *,
    content: str = "完成合同复核",
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "add_daily_items",
            "arguments": json.dumps(
                {
                    "date_selection": "server_default",
                    "items": [
                        {
                            "field": "today_work",
                            "content": content,
                            "source_evidence": {"source_message_index": 1},
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        },
    }


def _weekly_call(
    *,
    call_id: str = "weekly",
    plan_id: str | None = None,
    version: int = 0,
    plan_date: str = "2026-08-17",
    content: str = "整理案件材料",
    exact_clause_quote: str = "下周一整理案件材料",
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "apply_next_weekly_plan",
            "arguments": json.dumps(
                {
                    "plan_id": plan_id or _plan_id(),
                    "expected_version": version,
                    "operations": [
                        {
                            "operation_id": "add-monday",
                            "operation": "add",
                            "plan_date": plan_date,
                            "content": content,
                            "source_evidence": {
                                "source_message_index": 1,
                                "exact_clause_quote": exact_clause_quote,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        },
    }


def _assistant_tools(*calls: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def _terminal(*, actual_write: bool, outcome: str) -> dict:
    reply = (
        "你指的是本周还是下周？这次还没有写入。"
        if outcome == "needs_clarification"
        else ("已按你的意思处理。" if actual_write else "这次没有写入。")
    )
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "reply": reply,
                "actual_write": actual_write,
                "operation_outcome": outcome,
            },
            ensure_ascii=False,
        ),
    }


async def _run_ingress(
    monkeypatch,
    *,
    user_id: UUID = ALLOWED_USER_ID,
    conversation_kind: str = "direct",
    first_calls: tuple[dict, ...],
    reviewed_calls: tuple[dict, ...] | None,
    expected_write: bool = True,
    expected_outcome: str = "changed",
    user_text: str,
    now: datetime = NOW,
    message_occurred_at: datetime | None = None,
    settings: SimpleNamespace | None = None,
):
    session = _IngressSession()
    settings = settings or _settings()
    user = SimpleNamespace(
        id=user_id,
        name="测试用户",
        timezone="Asia/Shanghai",
        active=True,
        dingtalk_user_id=f"ding-{user_id}",
    )

    async def resolve_route(_session, **kwargs):
        capability = ProductionExecutionCapability(
            tenant_id=TENANT_ID,
            user_id=str(user_id),
            conversation_id=kwargs["conversation_id"],
            source_message_id=kwargs["source_message_id"],
            control_key=f"control-{user_id}",
            control_version=1,
            registry_digest=runtime_registry_contract_digest(settings),
            prompt_sha256=canary_prompt_sha256(),
            model_name=CANARY_MODEL_NAME,
            expires_at=now + timedelta(minutes=2),
            enabled=True,
            messages_enabled=True,
        )
        return canary_service.CanaryRouteResolution(
            decision=SimpleNamespace(
                owner="tool_call_core",
                reason="tool_call_canary_exact_identity",
            ),
            control=SimpleNamespace(messages_enabled=True),
            binding=SimpleNamespace(tenant_id=TENANT_ID),
            capability=capability,
        )

    scripted = [_assistant_tools(*first_calls)]
    if reviewed_calls is not None:
        scripted.append(_assistant_tools(*reviewed_calls))
    scripted.append(
        _terminal(actual_write=expected_write, outcome=expected_outcome)
    )
    http = _ScriptedHttpClient(scripted)
    monkeypatch.setattr(
        canary_service, "resolve_tool_call_canary_route", resolve_route
    )
    monkeypatch.setattr(canary_service, "ProductionRuntime", _RuntimeFactory)
    monkeypatch.setattr(
        canary_service, "_should_apply_personal_salutation", lambda _receipts: False
    )
    monkeypatch.setattr(
        canary_service, "_record_canary_metric_safely", lambda **_kwargs: None
    )

    outcome = await canary_service.process_tool_call_canary_ingress(
        session,
        user=user,
        dingtalk_user_id=user.dingtalk_user_id,
        user_text=user_text,
        source_channel="test",
        conversation_id=f"conversation-{user_id}",
        source_message_id="message-1",
        settings=settings,
        llm_client=SimpleNamespace(native_http_client=http),
        now=now,
        conversation_kind=conversation_kind,
        message_occurred_at=message_occurred_at or now,
    )
    return outcome, session, http


@pytest.mark.asyncio
async def test_private_daily_enters_daily_without_writing_weekly(monkeypatch) -> None:
    draft = _daily_call("draft-daily")
    reviewed = _daily_call("reviewed-daily")

    outcome, session, _http = await _run_ingress(
        monkeypatch,
        first_calls=(draft,),
        reviewed_calls=(reviewed,),
        user_text="今天完成合同复核",
    )

    assert outcome.owner == "tool_call_core"
    assert outcome.actual_write is True
    assert session.committed == {"daily": ["完成合同复核"], "weekly": []}


@pytest.mark.asyncio
async def test_production_prompt_is_built_from_this_users_allowed_tools(
    monkeypatch,
) -> None:
    captured_allowed_tools: list[frozenset[str] | None] = []
    real_prompt_builder = canary_service.canary_system_prompt

    def recording_prompt(*, allowed_tool_names=None):
        captured_allowed_tools.append(allowed_tool_names)
        return real_prompt_builder(allowed_tool_names=allowed_tool_names)

    monkeypatch.setattr(canary_service, "canary_system_prompt", recording_prompt)
    draft = _daily_call("draft-daily")
    reviewed = _daily_call("reviewed-daily")

    _outcome, _session, http = await _run_ingress(
        monkeypatch,
        first_calls=(draft,),
        reviewed_calls=(reviewed,),
        user_text="今天完成合同复核",
    )

    first_request_tools = frozenset(
        item["function"]["name"] for item in http.calls[0]["tools"]
    )
    assert captured_allowed_tools == [first_request_tools]
    assert "apply_next_weekly_plan" in first_request_tools
    assert http.calls[0]["messages"][0]["content"] == canary_system_prompt(
        allowed_tool_names=first_request_tools
    )


@pytest.mark.asyncio
async def test_friday_daily_prompt_is_not_affected_when_weekly_plan_is_closed(
    monkeypatch,
) -> None:
    draft = _daily_call("daily-only")

    outcome, session, http = await _run_ingress(
        monkeypatch,
        first_calls=(draft,),
        reviewed_calls=None,
        user_text="今天周五，完成合同复核",
        settings=_settings(
            weekly_plan_enabled=False,
            weekly_plan_write_enabled=False,
        ),
    )

    first_prompt = http.calls[0]["messages"][0]["content"]
    first_request_tools = {
        item["function"]["name"] for item in http.calls[0]["tools"]
    }
    assert outcome.actual_write is True
    assert session.committed == {"daily": ["完成合同复核"], "weekly": []}
    assert "Weekly Work Plan boundary:" not in first_prompt
    assert "apply_next_weekly_plan" not in first_request_tools
    assert len(http.calls) == 2


@pytest.mark.asyncio
async def test_private_weekly_enters_weekly_without_writing_daily(monkeypatch) -> None:
    draft = _weekly_call(call_id="draft-weekly")
    reviewed = _weekly_call(call_id="reviewed-weekly")

    outcome, session, _http = await _run_ingress(
        monkeypatch,
        first_calls=(draft,),
        reviewed_calls=(reviewed,),
        user_text="下周一整理案件材料",
    )

    assert outcome.actual_write is True
    assert session.committed == {"daily": [], "weekly": ["整理案件材料"]}


@pytest.mark.asyncio
async def test_private_dual_intent_enters_both_domains_in_one_commit(monkeypatch) -> None:
    draft = (_daily_call("draft-daily"), _weekly_call(call_id="draft-weekly"))
    reviewed = (
        _daily_call("reviewed-daily"),
        _weekly_call(call_id="reviewed-weekly"),
    )

    outcome, session, _http = await _run_ingress(
        monkeypatch,
        first_calls=draft,
        reviewed_calls=reviewed,
        user_text="今天完成合同复核；下周一整理案件材料",
    )

    assert outcome.actual_write is True
    assert session.committed == {
        "daily": ["完成合同复核"],
        "weekly": ["整理案件材料"],
    }
    assert session.outer_commit_count == 1


@pytest.mark.asyncio
async def test_group_weekly_is_rejected_but_group_daily_still_enters(monkeypatch) -> None:
    weekly_outcome, weekly_session, _ = await _run_ingress(
        monkeypatch,
        conversation_kind="group",
        first_calls=(_weekly_call(),),
        reviewed_calls=None,
        expected_write=False,
        expected_outcome="not_executed",
        user_text="下周一整理案件材料",
    )
    assert weekly_outcome.actual_write is False
    assert weekly_outcome.tool_blocked_count == 1
    assert weekly_session.committed == {"daily": [], "weekly": []}

    daily_outcome, daily_session, _ = await _run_ingress(
        monkeypatch,
        conversation_kind="group",
        first_calls=(_daily_call(),),
        reviewed_calls=None,
        user_text="今天完成合同复核",
    )
    assert daily_outcome.actual_write is True
    assert daily_session.committed == {"daily": ["完成合同复核"], "weekly": []}


@pytest.mark.asyncio
async def test_unknown_conversation_kind_cannot_enter_weekly_plan(monkeypatch) -> None:
    outcome, session, _ = await _run_ingress(
        monkeypatch,
        conversation_kind="unknown",
        first_calls=(_weekly_call(),),
        reviewed_calls=None,
        expected_write=False,
        expected_outcome="not_executed",
        user_text="下周一整理案件材料",
    )

    assert outcome.actual_write is False
    assert outcome.tool_blocked_count == 1
    assert session.committed == {"daily": [], "weekly": []}


@pytest.mark.asyncio
async def test_non_allowlisted_user_keeps_daily_but_cannot_enter_weekly(monkeypatch) -> None:
    daily_outcome, daily_session, _ = await _run_ingress(
        monkeypatch,
        user_id=OTHER_USER_ID,
        first_calls=(_daily_call(),),
        reviewed_calls=None,
        user_text="今天完成合同复核",
    )
    assert daily_outcome.actual_write is True
    assert daily_session.committed["daily"] == ["完成合同复核"]

    weekly_outcome, weekly_session, _ = await _run_ingress(
        monkeypatch,
        user_id=OTHER_USER_ID,
        first_calls=(_weekly_call(plan_id=_plan_id(OTHER_USER_ID)),),
        reviewed_calls=None,
        expected_write=False,
        expected_outcome="not_executed",
        user_text="下周一整理案件材料",
    )
    assert weekly_outcome.actual_write is False
    assert weekly_outcome.tool_blocked_count == 1
    assert weekly_session.committed["weekly"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_id", "version"),
    [
        ("50000000-0000-4000-8000-000000000001", 0),
        (_plan_id(), 7),
    ],
    ids=("untrusted-plan-id", "stale-plan-version"),
)
async def test_private_weekly_wrong_target_or_version_fails_closed(
    monkeypatch,
    plan_id: str,
    version: int,
) -> None:
    bad = _weekly_call(plan_id=plan_id, version=version)
    reviewed = _weekly_call(
        call_id="reviewed-bad-weekly", plan_id=plan_id, version=version
    )

    outcome, session, _ = await _run_ingress(
        monkeypatch,
        first_calls=(bad,),
        reviewed_calls=(reviewed,),
        expected_write=False,
        expected_outcome="not_executed",
        user_text="下周一整理案件材料",
    )

    assert outcome.actual_write is False
    assert outcome.tool_blocked_count == 1
    assert session.committed == {"daily": [], "weekly": []}


@pytest.mark.asyncio
async def test_monday_private_late_fill_selects_current_week_collection_target(
    monkeypatch,
) -> None:
    monday = datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI)
    current_week = _weekly_call(
        call_id="current-week",
        plan_id=_plan_id(target_week=date(2026, 8, 17)),
        plan_date="2026-08-19",
        content="做案件复盘",
        exact_clause_quote="补本周周三做案件复盘",
    )
    reviewed = _weekly_call(
        call_id="reviewed-current-week",
        plan_id=_plan_id(target_week=date(2026, 8, 17)),
        plan_date="2026-08-19",
        content="做案件复盘",
        exact_clause_quote="补本周周三做案件复盘",
    )

    outcome, session, _ = await _run_ingress(
        monkeypatch,
        now=monday,
        first_calls=(current_week,),
        reviewed_calls=(reviewed,),
        user_text="补本周周三做案件复盘",
    )

    assert outcome.actual_write is True
    assert session.committed == {"daily": [], "weekly": ["做案件复盘"]}


@pytest.mark.asyncio
async def test_monday_bare_weekday_asks_current_or_next_week_without_writing(
    monkeypatch,
) -> None:
    monday = datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI)
    ambiguous = _weekly_call(
        call_id="ambiguous-week",
        plan_id=_plan_id(target_week=date(2026, 8, 17)),
        plan_date="2026-08-19",
        content="整理证据",
        exact_clause_quote="周三整理证据",
    )
    reviewed = _weekly_call(
        call_id="reviewed-ambiguous-week",
        plan_id=_plan_id(target_week=date(2026, 8, 17)),
        plan_date="2026-08-19",
        content="整理证据",
        exact_clause_quote="周三整理证据",
    )

    outcome, session, _ = await _run_ingress(
        monkeypatch,
        now=monday,
        first_calls=(ambiguous,),
        reviewed_calls=(reviewed,),
        expected_write=False,
        expected_outcome="needs_clarification",
        user_text="周三整理证据",
    )

    assert outcome.actual_write is False
    assert outcome.tool_clarification_count == 1
    assert outcome.user_visible_result == "clarification"
    assert "本周" in outcome.message
    assert "下周" in outcome.message
    assert "没有写入" in outcome.message
    assert session.committed == {"daily": [], "weekly": []}


@pytest.mark.asyncio
async def test_monday_daily_plus_next_week_item_selects_natural_next_target(
    monkeypatch,
) -> None:
    monday = datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI)
    next_week_id = _plan_id(target_week=date(2026, 8, 24))
    draft = (
        _daily_call("monday-daily"),
        _weekly_call(
            call_id="natural-next",
            plan_id=next_week_id,
            plan_date="2026-08-26",
            content="提交案件材料",
            exact_clause_quote="下周三提交案件材料",
        ),
    )
    reviewed = (
        _daily_call("reviewed-monday-daily"),
        _weekly_call(
            call_id="reviewed-natural-next",
            plan_id=next_week_id,
            plan_date="2026-08-26",
            content="提交案件材料",
            exact_clause_quote="下周三提交案件材料",
        ),
    )

    outcome, session, _ = await _run_ingress(
        monkeypatch,
        now=monday,
        first_calls=draft,
        reviewed_calls=reviewed,
        user_text="今天完成合同复核；下周三提交案件材料",
    )

    assert outcome.actual_write is True
    assert session.committed == {
        "daily": ["完成合同复核"],
        "weekly": ["提交案件材料"],
    }


@pytest.mark.asyncio
async def test_weekly_report_wording_is_not_routed_to_weekly_plan(monkeypatch) -> None:
    daily = _daily_call(
        "weekly-report-daily", content="完成本周周报汇总"
    )
    reviewed = _daily_call(
        "reviewed-weekly-report-daily", content="完成本周周报汇总"
    )

    outcome, session, http = await _run_ingress(
        monkeypatch,
        first_calls=(daily,),
        reviewed_calls=(reviewed,),
        user_text="写进今天日报：完成本周周报汇总",
    )

    assert outcome.actual_write is True
    assert session.committed == {"daily": ["完成本周周报汇总"], "weekly": []}
    every_native_tool = [
        tool["function"]["name"]
        for request in http.calls
        for tool in request.get("tools", [])
        if tool.get("function", {}).get("name")
    ]
    # Weekly schemas may be available to the model, but no scripted model turn
    # or trusted receipt may convert the user's “周报” wording into a plan write.
    assert "apply_next_weekly_plan" in every_native_tool
    assert outcome.tool_success_count == 1


def test_model_contract_keeps_weekly_report_outside_weekly_work_plan() -> None:
    prompt = " ".join(canary_system_prompt().split())

    assert (
        "It is separate from a weekly report, six daily reports" in prompt
    )
    assert "A plan item is a commitment, not completion evidence" in prompt
    assert "never create a keyword route" in prompt


@pytest.mark.asyncio
async def test_sunday_message_processed_after_monday_midnight_keeps_original_natural_next_week(
    monkeypatch,
) -> None:
    server_now = datetime(2026, 8, 17, 0, 1, tzinfo=SHANGHAI)
    occurred_at = datetime(2026, 8, 16, 23, 59, tzinfo=SHANGHAI)
    call = _weekly_call(
        call_id="sunday-draft",
        plan_id=_plan_id(target_week=date(2026, 8, 17)),
        plan_date="2026-08-19",
        content="提交案件材料",
        exact_clause_quote="下周三提交案件材料",
    )
    reviewed = _weekly_call(
        call_id="sunday-reviewed",
        plan_id=_plan_id(target_week=date(2026, 8, 17)),
        plan_date="2026-08-19",
        content="提交案件材料",
        exact_clause_quote="下周三提交案件材料",
    )

    outcome, session, http = await _run_ingress(
        monkeypatch,
        now=server_now,
        message_occurred_at=occurred_at,
        first_calls=(call,),
        reviewed_calls=(reviewed,),
        user_text="下周三提交案件材料",
    )

    first_user_payload = json.loads(http.calls[0]["messages"][1]["content"])
    targets = first_user_payload["trusted_context"]["weekly_plan_targets"]
    assert len(targets) == 1
    assert targets[0]["target_week_start"] == "2026-08-17"
    assert "natural_next" in targets[0]["roles"]
    assert targets[0]["natural_next_for_message_indexes"] == [1]
    assert outcome.actual_write is True
    assert session.committed == {"daily": [], "weekly": ["提交案件材料"]}
