from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.report_sql_executor import periodic_report_id
from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionExecutionCapability,
)
from app.agent2.tool_calling.production_daily_executor import (
    ProductionExecutionError,
    ProductionHandlerOutcome,
)
from app.agent2.tool_calling.production_store import ProductionStateSnapshot
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.agent2.weekly_plan_domain import _stable_id

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 14, 18, 0, tzinfo=SHANGHAI)
TENANT_ID = "tenant-a"
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
TARGET_WEEK = date(2026, 8, 17)
PERIOD_KEY = "2026-W33"


class _EmptyResult:
    """Small read-only SQL result used by the real context assemblers."""

    rowcount = 0

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


class _ObservableSavepoint:
    def __init__(self, session: _ObservableSession) -> None:
        self._session = session
        self._before = deepcopy(session.working)
        self.is_active = True
        session._savepoints.append(self)

    async def commit(self) -> None:
        assert self.is_active
        assert self._session._savepoints[-1] is self
        self.is_active = False
        self._session._savepoints.pop()
        if not self._session._savepoints:
            self._session.committed = deepcopy(self._session.working)
            self._session.outer_commit_count += 1

    async def rollback(self) -> None:
        assert self.is_active
        assert self._session._savepoints[-1] is self
        self._session.working = deepcopy(self._before)
        self.is_active = False
        self._session._savepoints.pop()
        if not self._session._savepoints:
            self._session.outer_rollback_count += 1


class _ObservableSession:
    """Observable transaction boundary; it never connects to a real database."""

    def __init__(self, *, fail_domain: str | None = None) -> None:
        self.working = {
            "daily_reports": [],
            "periodic_reports": [],
            "weekly_plans": [],
            "clear_pendings": [],
            "personal_memories": [],
            "personal_memory_audits": [],
            "receipts": [],
        }
        self.committed = deepcopy(self.working)
        self.fail_domain = fail_domain
        self.attempted_calls: list[str] = []
        self.outer_commit_count = 0
        self.outer_rollback_count = 0
        self._savepoints: list[_ObservableSavepoint] = []

    async def scalar(self, _statement):
        return None

    async def scalars(self, _statement):
        return _EmptyResult()

    async def execute(self, _statement, _parameters=None):
        return _EmptyResult()

    async def begin_nested(self):
        return _ObservableSavepoint(self)

    async def flush(self) -> None:
        return None

    def state(self) -> ProductionStateSnapshot:
        canonical = json.dumps(
            self.working,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ProductionStateSnapshot(
            canonical_json=canonical,
            canonical_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


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


class _ObservableDailyExecutor:
    def __init__(self, *, session, **_kwargs) -> None:
        self._session = session

    async def add_daily_items(self, request) -> ProductionHandlerOutcome:
        self._session.attempted_calls.append(request.tool_name)
        if self._session.fail_domain == "daily":
            raise ProductionExecutionError("TEST_DAILY_WRITE_FAILED")
        contents = [item.content for item in request.arguments.items]
        self._session.working["daily_reports"].extend(contents)
        return _write_outcome(
            target_type="daily_report",
            target_id=f"daily:{NOW.date().isoformat()}",
        )


class _ObservablePeriodicExecutor:
    def __init__(self, *, session, **_kwargs) -> None:
        self._session = session

    async def query_current_weekly_report(
        self, request
    ) -> ProductionHandlerOutcome:
        self._session.attempted_calls.append(request.tool_name)
        return ProductionHandlerOutcome(
            target_type="periodic_report",
            target_id=str(_periodic_report_id()),
            before_report=None,
            after_report=None,
            idempotency_key=None,
            before_version=0,
            after_version=0,
            safe_user_facts={
                "actual_write": False,
                "authoritative_read_response": True,
                "model_composition_allowed": False,
                "response_text": "本周周报目前还是草稿，尚无内容。",
            },
            status_if_unchanged=ReceiptStatus.SUCCESS,
        )

    async def apply_current_weekly_report(
        self, request
    ) -> ProductionHandlerOutcome:
        self._session.attempted_calls.append(request.tool_name)
        if self._session.fail_domain == "periodic_report":
            raise ProductionExecutionError("TEST_PERIODIC_REPORT_WRITE_FAILED")
        contents = [
            operation.content
            for operation in request.arguments.operations
            if operation.operation == "append"
        ]
        self._session.working["periodic_reports"].extend(contents)
        return _write_outcome(
            target_type="periodic_report",
            target_id=str(_periodic_report_id()),
        )

    async def submit_current_weekly_report(
        self, request
    ) -> ProductionHandlerOutcome:
        self._session.attempted_calls.append(request.tool_name)
        if self._session.fail_domain == "periodic_report":
            raise ProductionExecutionError("TEST_PERIODIC_REPORT_WRITE_FAILED")
        self._session.working["periodic_reports"].append("submitted")
        return _write_outcome(
            target_type="periodic_report",
            target_id=str(_periodic_report_id()),
        )


class _ObservableWeeklyPlanExecutor:
    def __init__(self, *, session, **_kwargs) -> None:
        self._session = session

    async def apply_next_weekly_plan(self, request) -> ProductionHandlerOutcome:
        self._session.attempted_calls.append(request.tool_name)
        if self._session.fail_domain == "weekly_plan":
            raise ProductionExecutionError("TEST_WEEKLY_PLAN_WRITE_FAILED")
        contents = [
            operation.content
            for operation in request.arguments.operations
            if operation.operation == "add"
        ]
        self._session.working["weekly_plans"].extend(contents)
        return _write_outcome(
            target_type="weekly_plan",
            target_id=str(request.arguments.plan_id),
        )


def _write_outcome(*, target_type: str, target_id: str) -> ProductionHandlerOutcome:
    return ProductionHandlerOutcome(
        target_type=target_type,
        target_id=target_id,
        before_report=None,
        after_report=None,
        idempotency_key=f"test:{target_type}:{target_id}",
        before_version=0,
        after_version=1,
        safe_user_facts={"actual_write": True},
    )


def _periodic_report_id() -> UUID:
    return periodic_report_id(
        TENANT_ID,
        str(USER_ID),
        "weekly",
        PERIOD_KEY,
    )


def _plan_id() -> str:
    return _stable_id(
        "weekly-plan",
        TENANT_ID,
        str(USER_ID),
        TARGET_WEEK.isoformat(),
    )


def _native_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    }


def _periodic_query_call(call_id: str = "periodic-query") -> dict:
    return _native_call(call_id, "query_current_weekly_report", {})


def _periodic_apply_call(call_id: str = "periodic-apply") -> dict:
    return _native_call(
        call_id,
        "apply_current_weekly_report",
        {
            "report_id": str(_periodic_report_id()),
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "periodic-append-1",
                    "operation": "append",
                    "field": "accomplishments",
                    "content": "完成合同风险清单",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        },
    )


def _periodic_submit_call(call_id: str = "periodic-submit") -> dict:
    return _native_call(
        call_id,
        "submit_current_weekly_report",
        {
            "report_id": str(_periodic_report_id()),
            "expected_version": 0,
            "confirmation_evidence": {"source_message_index": 1},
        },
    )


def _daily_call(call_id: str = "daily") -> dict:
    return _native_call(
        call_id,
        "add_daily_items",
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": "今天完成合同复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天完成合同复核",
                    },
                }
            ],
        },
    )


def _daily_rephrased_call(call_id: str) -> dict:
    return _native_call(
        call_id,
        "add_daily_items",
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": "完成日报基础功能优化",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天做了日报的基础功能优化",
                    },
                }
            ],
        },
    )


def _daily_three_field_rephrased_call(call_id: str) -> dict:
    return _native_call(
        call_id,
        "add_daily_items",
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": "已完成合同复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同复核",
                    },
                },
                {
                    "field": "today_work",
                    "content": "完成日报台账整理",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "整理日报台账",
                    },
                },
                {
                    "field": "problems",
                    "content": "供应商材料不齐风险",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "供应商材料尚未齐全",
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "content": "推进付款审批",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "跟进付款审批",
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "content": "完善风险清单",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "更新风险清单",
                    },
                },
            ],
        },
    )


def _weekly_plan_call(call_id: str = "weekly-plan") -> dict:
    return _native_call(
        call_id,
        "apply_next_weekly_plan",
        {
            "plan_id": _plan_id(),
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "weekly-plan-add-1",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "整理案件材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "下周一整理案件材料",
                    },
                }
            ],
        },
    )


def _assistant_tools(*calls: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def _write_terminal() -> dict:
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "reply": "已按你的原话分别记入相应记录。",
                "actual_write": True,
                "operation_outcome": "changed",
            },
            ensure_ascii=False,
        ),
    }


def _read_terminal() -> dict:
    return {"role": "assistant", "content": "这是你本周的周报。"}


def _settings(*, current_weekly_report_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        timezone="Asia/Shanghai",
        llm_base_url="https://example.invalid",
        agent2_weekly_plan_enabled=True,
        agent2_weekly_plan_write_enabled=True,
        agent2_weekly_plan_send_enabled=False,
        agent2_weekly_plan_tenant_allowlist=TENANT_ID,
        agent2_weekly_plan_user_allowlist=str(USER_ID),
        agent2_weekly_plan_send_user_allowlist="",
        agent2_current_weekly_report_enabled=current_weekly_report_enabled,
        agent2_current_weekly_report_tenant_allowlist=TENANT_ID,
        agent2_current_weekly_report_user_allowlist=str(USER_ID),
        legal_daily_dashboard_enabled=False,
        agent2_cross_user_daily_read_enabled=False,
        agent2_performance_tool_enabled=False,
        legal_ops_data_intake_enabled=False,
        agent2_performance_knowledge_enabled=False,
    )


def _install_observable_runtime(monkeypatch) -> None:
    import app.agent2.tool_calling.production_runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "ProductionDailyExecutor",
        _ObservableDailyExecutor,
    )
    monkeypatch.setattr(
        runtime_module,
        "ProductionPeriodicReportExecutor",
        _ObservablePeriodicExecutor,
    )
    monkeypatch.setattr(
        runtime_module,
        "ProductionWeeklyPlanExecutor",
        _ObservableWeeklyPlanExecutor,
    )

    async def _control_is_open(_self) -> bool:
        return True

    async def _lock_turn(_self) -> None:
        return None

    async def _load_replays(_self, prepared):
        return tuple(None for _ in prepared)

    async def _state(self):
        return self._session.state()

    async def _persist_receipt(
        self,
        *,
        item,
        outcome,
        before,
        after,
        changed,
    ) -> ToolReceipt:
        del before, after
        status = ReceiptStatus.SUCCESS if changed else outcome.status_if_unchanged
        safe_facts = dict(outcome.safe_user_facts or {})
        safe_facts["actual_write"] = changed
        receipt = ToolReceipt(
            status=status,
            tool_name=item.bound.call.tool_name,
            changed=changed,
            target_type=outcome.target_type,
            target_id=outcome.target_id,
            before_version=outcome.before_version,
            after_version=outcome.after_version,
            affected_item_ids=outcome.affected_item_ids,
            safe_user_facts=safe_facts,
            server_evidence={"receipt_id": f"test:{item.bound.call.tool_call_id}"},
            execution_mode=ExecutionMode.CANARY_EXECUTE,
            idempotency_key=outcome.idempotency_key,
        )
        self._session.working["receipts"].append(
            {
                "tool_call_id": item.bound.call.tool_call_id,
                "tool_name": item.bound.call.tool_name,
                "changed": changed,
            }
        )
        return receipt

    monkeypatch.setattr(
        runtime_module.ProductionRuntimeSession,
        "_control_is_open",
        _control_is_open,
    )
    monkeypatch.setattr(
        runtime_module.ProductionRuntimeSession,
        "_lock_turn",
        _lock_turn,
    )
    monkeypatch.setattr(
        runtime_module.ProductionRuntimeSession,
        "_load_replays",
        _load_replays,
    )
    monkeypatch.setattr(
        runtime_module.ProductionRuntimeSession,
        "_state",
        _state,
    )
    monkeypatch.setattr(
        runtime_module.ProductionRuntimeSession,
        "_persist_and_verify_receipt",
        _persist_receipt,
    )


async def _run_ingress(
    monkeypatch,
    *,
    user_text: str,
    first_calls: tuple[dict, ...],
    reviewed_calls: tuple[dict, ...] | None = None,
    terminal: dict | None = None,
    fail_domain: str | None = None,
    settings: SimpleNamespace | None = None,
):
    _install_observable_runtime(monkeypatch)
    session = _ObservableSession(fail_domain=fail_domain)
    settings = settings or _settings()
    user = SimpleNamespace(
        id=USER_ID,
        name="测试用户",
        timezone="Asia/Shanghai",
        active=True,
        team_id=UUID("20000000-0000-4000-8000-000000000001"),
        dingtalk_user_id="ding-test-user",
    )

    async def resolve_route(_session, **kwargs):
        capability = ProductionExecutionCapability(
            tenant_id=TENANT_ID,
            user_id=str(USER_ID),
            conversation_id=kwargs["conversation_id"],
            source_message_id=kwargs["source_message_id"],
            control_key="test-control",
            control_version=1,
            registry_digest=runtime_registry_contract_digest(settings),
            prompt_sha256=canary_prompt_sha256(),
            model_name=CANARY_MODEL_NAME,
            expires_at=NOW + timedelta(minutes=2),
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
    elif any(
        call["function"]["name"]
        in {
            "query_current_weekly_report",
            "apply_current_weekly_report",
            "submit_current_weekly_report",
        }
        for call in first_calls
    ):
        scripted.append(_assistant_tools(*first_calls))
    if terminal is not None:
        scripted.append(terminal)
    http = _ScriptedHttpClient(scripted)
    monkeypatch.setattr(
        canary_service,
        "resolve_tool_call_canary_route",
        resolve_route,
    )
    monkeypatch.setattr(
        canary_service,
        "_should_apply_personal_salutation",
        lambda _receipts: False,
    )
    monkeypatch.setattr(
        canary_service,
        "_record_canary_metric_safely",
        lambda **_kwargs: None,
    )

    outcome = await canary_service.process_tool_call_canary_ingress(
        session,
        user=user,
        dingtalk_user_id=user.dingtalk_user_id,
        user_text=user_text,
        source_channel="test",
        conversation_id="direct-test-user",
        source_message_id="message-1",
        settings=settings,
        llm_client=SimpleNamespace(native_http_client=http),
        now=NOW,
        conversation_kind="direct",
        message_occurred_at=NOW,
    )
    return outcome, session, http


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "user_text", "terminal", "expected_write", "expected_value"),
    (
        (
            _periodic_query_call(),
            "查看我本周周报",
            _read_terminal(),
            False,
            [],
        ),
        (
            _periodic_apply_call(),
            "本周周报补充：完成合同风险清单",
            _write_terminal(),
            True,
            ["完成合同风险清单"],
        ),
        (
            _periodic_submit_call(),
            "确认提交本周周报",
            _write_terminal(),
            True,
            ["submitted"],
        ),
    ),
    ids=("query", "apply", "submit"),
)
async def test_pure_weekly_report_uses_real_tool_call_core_ingress(
    monkeypatch,
    call,
    user_text,
    terminal,
    expected_write,
    expected_value,
) -> None:
    outcome, session, http = await _run_ingress(
        monkeypatch,
        user_text=user_text,
        first_calls=(call,),
        terminal=terminal,
    )

    tool_name = call["function"]["name"]
    assert outcome.owner == "tool_call_core"
    assert outcome.actual_write is expected_write
    assert outcome.tool_success_count == 1
    assert session.committed["periodic_reports"] == expected_value
    assert session.attempted_calls == [tool_name]
    assert [item["tool_name"] for item in session.committed["receipts"]] == [
        tool_name
    ]
    assert session.outer_commit_count == 1
    assert session.outer_rollback_count == 0
    supplied_tools = {
        item["function"]["name"] for item in http.calls[0]["tools"]
    }
    assert {
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
    }.issubset(supplied_tools)


@pytest.mark.asyncio
async def test_production_prompt_omits_current_weekly_report_policy_when_closed(
    monkeypatch,
) -> None:
    outcome, session, http = await _run_ingress(
        monkeypatch,
        user_text="今天完成合同复核",
        first_calls=(_daily_call(),),
        reviewed_calls=(_daily_call("reviewed-daily"),),
        terminal=_write_terminal(),
        settings=_settings(current_weekly_report_enabled=False),
    )

    first_prompt = http.calls[0]["messages"][0]["content"]
    supplied_tools = {
        item["function"]["name"] for item in http.calls[0]["tools"]
    }
    assert outcome.actual_write is True
    assert session.committed["daily_reports"] == ["今天完成合同复核"]
    assert "Current Weekly Report boundary:" not in first_prompt
    assert "query_current_weekly_report" not in supplied_tools


@pytest.mark.asyncio
async def test_daily_ingress_persists_server_owned_quote_when_model_rephrases(
    monkeypatch,
) -> None:
    original_user_text = "今天做了日报的基础功能优化"

    outcome, session, _ = await _run_ingress(
        monkeypatch,
        user_text=original_user_text,
        first_calls=(_daily_rephrased_call("draft-daily-rephrased"),),
        reviewed_calls=(_daily_rephrased_call("reviewed-daily-rephrased"),),
        terminal=_write_terminal(),
    )

    assert outcome.actual_write is True
    assert session.committed["daily_reports"] == ["今天做了日报的基础功能优化"]


@pytest.mark.asyncio
async def test_daily_ingress_copies_each_multi_field_item_from_its_own_quote(
    monkeypatch,
) -> None:
    user_text = (
        "今日工作：完成合同复核、整理日报台账；"
        "问题风险：供应商材料尚未齐全；"
        "明日计划：跟进付款审批、更新风险清单"
    )

    outcome, session, _ = await _run_ingress(
        monkeypatch,
        user_text=user_text,
        first_calls=(_daily_three_field_rephrased_call("draft-daily-three-field"),),
        reviewed_calls=(
            _daily_three_field_rephrased_call("reviewed-daily-three-field"),
        ),
        terminal=_write_terminal(),
    )

    assert outcome.actual_write is True
    assert session.committed["daily_reports"] == [
        "完成合同复核",
        "整理日报台账",
        "供应商材料尚未齐全",
        "跟进付款审批",
        "更新风险清单",
    ]


@pytest.mark.asyncio
async def test_one_message_can_update_weekly_report_and_weekly_plan_atomically(
    monkeypatch,
) -> None:
    outcome, session, _ = await _run_ingress(
        monkeypatch,
        user_text=(
            "本周周报补充：完成合同风险清单；"
            "下周一整理案件材料"
        ),
        first_calls=(
            _periodic_apply_call("draft-periodic"),
            _weekly_plan_call("draft-weekly-plan"),
        ),
            reviewed_calls=(
                _periodic_apply_call("reviewed-periodic"),
                _weekly_plan_call("reviewed-weekly-plan"),
            ),
        terminal=_write_terminal(),
    )

    assert outcome.actual_write is True
    assert session.committed["daily_reports"] == []
    assert session.committed["periodic_reports"] == ["完成合同风险清单"]
    assert session.committed["weekly_plans"] == ["整理案件材料"]
    assert [item["tool_name"] for item in session.committed["receipts"]] == [
        "apply_current_weekly_report",
        "apply_next_weekly_plan",
    ]
    assert session.outer_commit_count == 1
    assert session.outer_rollback_count == 0


@pytest.mark.asyncio
async def test_one_message_can_update_daily_weekly_report_and_weekly_plan_atomically(
    monkeypatch,
) -> None:
    outcome, session, _ = await _run_ingress(
        monkeypatch,
        user_text=(
            "今天完成合同复核；本周周报补充：完成合同风险清单；"
            "下周一整理案件材料"
        ),
        first_calls=(
            _daily_call("draft-daily"),
            _periodic_apply_call("draft-periodic"),
            _weekly_plan_call("draft-weekly-plan"),
        ),
        reviewed_calls=(
            _daily_call("reviewed-daily"),
            _periodic_apply_call("reviewed-periodic"),
            _weekly_plan_call("reviewed-weekly-plan"),
        ),
        terminal=_write_terminal(),
    )

    assert outcome.actual_write is True
    assert session.committed["daily_reports"] == ["今天完成合同复核"]
    assert session.committed["periodic_reports"] == ["完成合同风险清单"]
    assert session.committed["weekly_plans"] == ["整理案件材料"]
    assert {item["tool_name"] for item in session.committed["receipts"]} == {
        "add_daily_items",
        "apply_current_weekly_report",
        "apply_next_weekly_plan",
    }
    assert session.outer_commit_count == 1
    assert session.outer_rollback_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_domain", "draft_calls", "reviewed_calls", "expected_order"),
    (
        (
            "daily",
            (
                _periodic_apply_call("draft-periodic"),
                _weekly_plan_call("draft-weekly-plan"),
                _daily_call("draft-daily"),
            ),
                (
                    _periodic_apply_call("reviewed-periodic"),
                    _weekly_plan_call("reviewed-weekly-plan"),
                    _daily_call("reviewed-daily"),
                ),
            (
                "apply_current_weekly_report",
                "apply_next_weekly_plan",
                "add_daily_items",
            ),
        ),
        (
            "periodic_report",
            (
                _daily_call("draft-daily"),
                _weekly_plan_call("draft-weekly-plan"),
                _periodic_apply_call("draft-periodic"),
            ),
                (
                    _daily_call("reviewed-daily"),
                    _weekly_plan_call("reviewed-weekly-plan"),
                    _periodic_apply_call("reviewed-periodic"),
                ),
            (
                "add_daily_items",
                "apply_next_weekly_plan",
                "apply_current_weekly_report",
            ),
        ),
        (
            "weekly_plan",
            (
                _periodic_apply_call("draft-periodic"),
                _daily_call("draft-daily"),
                _weekly_plan_call("draft-weekly-plan"),
            ),
                (
                    _periodic_apply_call("reviewed-periodic"),
                    _daily_call("reviewed-daily"),
                    _weekly_plan_call("reviewed-weekly-plan"),
                ),
            (
                "apply_current_weekly_report",
                "add_daily_items",
                "apply_next_weekly_plan",
            ),
        ),
    ),
    ids=("daily-fails-last", "weekly-report-fails-last", "weekly-plan-fails-last"),
)
async def test_any_domain_failure_rolls_back_all_three_domains_and_receipts(
    monkeypatch,
    fail_domain,
    draft_calls,
    reviewed_calls,
    expected_order,
) -> None:
    outcome, session, _ = await _run_ingress(
        monkeypatch,
        user_text=(
            "今天完成合同复核；本周周报补充：完成合同风险清单；"
            "下周一整理案件材料"
        ),
        first_calls=draft_calls,
        reviewed_calls=reviewed_calls,
        fail_domain=fail_domain,
    )

    assert outcome.owner == "blocked"
    assert outcome.actual_write is False
    assert outcome.user_visible_result == "failed"
    assert tuple(session.attempted_calls) == expected_order
    assert session.working == {
        "daily_reports": [],
        "periodic_reports": [],
        "weekly_plans": [],
        "clear_pendings": [],
        "personal_memories": [],
        "personal_memory_audits": [],
        "receipts": [],
    }
    assert session.committed == session.working
    assert session.outer_commit_count == 0
    assert session.outer_rollback_count == 1
