from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

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
    ProductionRuntimeResult,
)
from app.agent2.tool_calling.production_runtime import (
    ProductionRuntime as RealProductionRuntime,
)
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.agent2.weekly_plan_domain import (
    _stable_id,
    create_weekly_plan,
    create_weekly_plan_batch,
)
from app.agent2.weekly_plan_models import (
    WeeklyPlanItem,
    WeeklyPlanRosterMember,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 14, 16, 0, tzinfo=SHANGHAI)
TENANT_ID = "tenant-cross-turn"
USER_ID = UUID("10000000-0000-4000-8000-000000000011")
CONVERSATION_ID = "direct-cross-turn-conversation"
TARGET_WEEK = date(2026, 8, 17)
DAILY_REPORT_ID = UUID("20000000-0000-4000-8000-000000000011")


class _Rows:
    def __init__(self, rows=(), *, scalar=None) -> None:
        self._rows = list(rows)
        self._scalar = scalar

    def scalars(self):
        return self

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def one_or_none(self):
        if len(self._rows) > 1:
            raise AssertionError("test query unexpectedly returned multiple rows")
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._scalar


def _entity_names(statement) -> set[str]:
    result: set[str] = set()
    for description in getattr(statement, "column_descriptions", ()):
        entity = description.get("entity")
        name = getattr(entity, "__name__", "")
        if name:
            result.add(name)
    return result


class _Savepoint:
    def __init__(self, session: _ConversationSession) -> None:
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


class _ConversationSession:
    """Test database boundary with durable weekly and conversation snapshots."""

    def __init__(self) -> None:
        self.batch = create_weekly_plan_batch(
            tenant_id=TENANT_ID,
            target_week_start=TARGET_WEEK,
            roster=(
                WeeklyPlanRosterMember(
                    user_id=str(USER_ID),
                    display_name="测试用户",
                ),
            ),
            created_at=NOW,
        )
        self.working = {
            "daily": [],
            "daily_version": 0,
            "weekly_plan": None,
            "receipts": [],
        }
        self.committed = deepcopy(self.working)
        self.events: list[SimpleNamespace] = []
        self.outer_commit_count = 0
        self.outer_rollback_count = 0

    async def begin_nested(self):
        return _Savepoint(self)

    async def scalar(self, _statement):
        return None

    async def scalars(self, statement):
        names = _entity_names(statement)
        if "WebhookEvent" in names:
            return _Rows(reversed(self.events))
        if "ToolCallCanaryReceipt" in names:
            return _Rows(reversed(self.committed["receipts"]))
        return _Rows()

    async def execute(self, statement, _parameters=None):
        sql = str(statement).lower()
        plan = self.committed["weekly_plan"]
        if (
            "agent2_weekly_plan_batches join "
            "agent2_weekly_plan_roster_members" in sql
        ):
            return _Rows(
                (
                    {
                        "batch_id": UUID(self.batch.batch_id),
                        "target_week_start": self.batch.target_week_start,
                    },
                )
            )
        if "from agent2_weekly_plans" in sql:
            if plan is None:
                return _Rows()
            selected = tuple(
                getattr(column, "key", "")
                for column in getattr(statement, "selected_columns", ())
            )
            if selected == ("plan_id",):
                return _Rows(scalar=UUID(plan.plan_id))
            return _Rows(
                (
                    {
                        "plan_id": UUID(plan.plan_id),
                        "tenant_id": plan.tenant_id,
                        "batch_id": UUID(plan.batch_id),
                        "owner_user_id": plan.owner_user_id,
                        "target_week_start": plan.target_week_start,
                        "status": plan.status,
                        "version": plan.version,
                        "submitted_at": plan.submitted_at,
                        "created_at": plan.created_at,
                        "updated_at": plan.updated_at,
                    },
                )
            )
        if "from agent2_weekly_plan_days" in sql:
            if plan is None:
                return _Rows()
            return _Rows(
                {
                    "day_id": UUID(day.day_id),
                    "tenant_id": plan.tenant_id,
                    "plan_id": UUID(plan.plan_id),
                    "plan_date": day.plan_date,
                    "day_index": index,
                    "state": day.state,
                    "created_at": plan.created_at,
                    "updated_at": plan.updated_at,
                }
                for index, day in enumerate(plan.days, start=1)
            )
        if "from agent2_weekly_plan_items" in sql:
            if plan is None:
                return _Rows()
            return _Rows(
                {
                    "item_id": UUID(item.item_id),
                    "tenant_id": plan.tenant_id,
                    "plan_id": UUID(plan.plan_id),
                    "day_id": UUID(day.day_id),
                    "original_text": item.original_text,
                    "source": item.source,
                    "source_ref": item.source_ref,
                    "position": position,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "deleted_at": None,
                }
                for day in plan.days
                for position, item in enumerate(day.items, start=1)
            )
        if "from agent2_weekly_plan_suggestions" in sql:
            return _Rows()
        return _Rows()

    def remember_turn(
        self,
        *,
        source_message_id: str,
        user_text: str,
        assistant_text: str,
        occurred_at: datetime,
    ) -> None:
        self.events.append(
            SimpleNamespace(
                id=UUID(_stable_id("cross-turn-event", source_message_id)),
                idempotency_key=source_message_id,
                dingtalk_user_id=f"ding-{USER_ID}",
                status="processed",
                payload={
                    "conversationId": CONVERSATION_ID,
                    "text": {"content": user_text},
                },
                response_payload={"text": {"content": assistant_text}},
                received_at=occurred_at,
            )
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


class _ScriptedModel:
    """A sequence-only model boundary; it never routes from words or phrases."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = iter(messages)
        self.calls: list[dict] = []

    async def post(self, _endpoint, *, json, timeout):
        del timeout
        self.calls.append(json)
        return _HttpResponse(next(self._messages), len(self.calls))


class _PendingRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self, real_session, session: _ConversationSession) -> None:
        self._real = real_session
        self._session = session
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

        before = deepcopy(self._session.working)
        receipts: list[ToolReceipt] = []
        for item in bound:
            call = item.call
            if call.tool_name == "add_daily_items":
                before_version = self._session.working["daily_version"]
                contents = [entry["content"] for entry in call.arguments["items"]]
                self._session.working["daily"].extend(contents)
                self._session.working["daily_version"] += 1
                target_type = "daily_report"
                target_id = str(DAILY_REPORT_ID)
                after_version = self._session.working["daily_version"]
                affected_item_ids = tuple(
                    _stable_id("cross-turn-daily-item", call.tool_call_id, index)
                    for index, _ in enumerate(contents, start=1)
                )
            elif call.tool_name == "record_weekly_plan_items_as_today_work":
                plan = self._session.working["weekly_plan"]
                if plan is None:
                    raise AssertionError("trusted weekly reference requires a live plan")
                if (
                    call.arguments["plan_id"] != plan.plan_id
                    or call.arguments["expected_version"] != plan.version
                ):
                    raise AssertionError("trusted weekly reference is stale")
                selected_ids = set(call.arguments["target_item_ids"])
                selected = [
                    item.original_text
                    for day in plan.days
                    for item in day.items
                    if item.item_id in selected_ids
                ]
                if len(selected) != len(selected_ids):
                    raise AssertionError("trusted weekly item is missing")
                before_version = self._session.working["daily_version"]
                contents = list(dict.fromkeys(selected))
                existing = set(self._session.working["daily"])
                additions = [content for content in contents if content not in existing]
                self._session.working["daily"].extend(additions)
                self._session.working["daily_version"] += 1
                target_type = "daily_report"
                target_id = str(DAILY_REPORT_ID)
                after_version = self._session.working["daily_version"]
                affected_item_ids = tuple(
                    _stable_id("cross-turn-daily-item", call.tool_call_id, index)
                    for index, _ in enumerate(additions, start=1)
                )
            elif call.tool_name == "apply_next_weekly_plan":
                plan = self._session.working["weekly_plan"]
                if plan is None:
                    plan = create_weekly_plan(
                        batch=self._session.batch,
                        owner_user_id=str(USER_ID),
                        created_at=self._real._context.now,
                    )
                before_version = plan.version
                if call.arguments["expected_version"] != before_version:
                    raise AssertionError("test writer received a stale weekly version")
                days = list(plan.days)
                affected = []
                for operation in call.arguments["operations"]:
                    if operation["operation"] != "add":
                        raise AssertionError("test writer only supports observable adds")
                    day_index = next(
                        index
                        for index, day in enumerate(days)
                        if day.plan_date.isoformat() == operation["plan_date"]
                    )
                    day = days[day_index]
                    item_id = _stable_id(
                        "cross-turn-weekly-item",
                        call.tool_call_id,
                        operation["operation_id"],
                    )
                    affected.append(item_id)
                    new_item = WeeklyPlanItem(
                        item_id=item_id,
                        original_text=operation["content"],
                        source="manual",
                        source_ref="",
                        created_at=self._real._context.now,
                        updated_at=self._real._context.now,
                    )
                    days[day_index] = replace(
                        day,
                        state="planned",
                        items=(*day.items, new_item),
                    )
                plan = replace(
                    plan,
                    version=plan.version + 1,
                    days=tuple(days),
                    updated_at=self._real._context.now,
                )
                self._session.working["weekly_plan"] = plan
                target_type = "weekly_plan"
                target_id = plan.plan_id
                after_version = plan.version
                affected_item_ids = tuple(affected)
            else:
                raise AssertionError(f"unexpected scripted tool: {call.tool_name}")

            receipt = ToolReceipt(
                status=ReceiptStatus.SUCCESS,
                tool_name=call.tool_name,
                changed=True,
                target_type=target_type,
                target_id=target_id,
                before_version=before_version,
                after_version=after_version,
                affected_item_ids=affected_item_ids,
                safe_user_facts={"actual_write": True},
                execution_mode=ExecutionMode.CANARY_EXECUTE,
            )
            receipts.append(receipt)
            principal = self._real._context.principal
            self._session.working["receipts"].append(
                SimpleNamespace(
                    tenant_id=principal.tenant_id,
                    user_id=str(principal.user_id),
                    conversation_id=principal.conversation_id,
                    source_message_id=principal.source_message_id,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    status="success",
                    changed=True,
                    target_type=target_type,
                    target_id=target_id,
                    before_version=before_version,
                    after_version=after_version,
                    affected_item_ids=list(affected_item_ids),
                    safe_user_facts={},
                    created_at=self._real._context.now,
                )
            )

        receipt_tuple = tuple(receipts)
        if defer_finalization:
            self._pending = receipt_tuple
            self._pending_before = before
            return ProductionRuntimeResult(
                status="success",
                receipts=receipt_tuple,
                transaction_opened=True,
                transaction_pending=True,
            )
        self._session.committed = deepcopy(self._session.working)
        return ProductionRuntimeResult(
            status="success",
            receipts=receipt_tuple,
            transaction_opened=True,
            committed_to_outer_transaction=True,
            business_write_count=len(receipt_tuple),
        )

    async def commit_pending(self) -> ProductionRuntimeResult:
        assert self._pending is not None
        receipts = self._pending
        self._pending = None
        self._pending_before = None
        self._session.committed = deepcopy(self._session.working)
        return ProductionRuntimeResult(
            status="success",
            receipts=receipts,
            transaction_opened=True,
            committed_to_outer_transaction=True,
            business_write_count=len(receipts),
        )

    async def rollback_pending(self) -> None:
        if self._pending_before is not None:
            self._session.working = deepcopy(self._pending_before)
        self._pending = None
        self._pending_before = None


class _RuntimeFactory:
    def open_session(self, **kwargs):
        real = RealProductionRuntime().open_session(**kwargs)
        return _PendingRuntime(real, kwargs["session"])


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        timezone="Asia/Shanghai",
        llm_base_url="https://example.invalid",
        agent2_weekly_plan_enabled=True,
        agent2_weekly_plan_write_enabled=True,
        agent2_weekly_plan_send_enabled=False,
        agent2_weekly_plan_tenant_allowlist=TENANT_ID,
        agent2_weekly_plan_user_allowlist=str(USER_ID),
        agent2_weekly_plan_send_user_allowlist="",
        agent2_current_weekly_report_enabled=False,
        agent2_current_weekly_report_tenant_allowlist="",
        agent2_current_weekly_report_user_allowlist="",
        legal_daily_dashboard_enabled=False,
        agent2_cross_user_daily_read_enabled=False,
        agent2_performance_tool_enabled=False,
        legal_ops_data_intake_enabled=False,
        agent2_performance_knowledge_enabled=False,
    )


def _assistant_tools(*calls: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def _terminal(*, reply: str, actual_write: bool, outcome: str) -> dict:
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


def _daily_call(call_id: str, *, content: str) -> dict:
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


def _weekly_item_to_daily_call(
    call_id: str,
    *,
    plan_id: str,
    expected_version: int,
    target_item_ids: tuple[str, ...],
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "record_weekly_plan_items_as_today_work",
            "arguments": json.dumps(
                {
                    "plan_id": plan_id,
                    "expected_version": expected_version,
                    "target_item_ids": list(target_item_ids),
                    "source_evidence": {"source_message_index": 1},
                }
            ),
        },
    }


def _weekly_call(
    call_id: str,
    *,
    expected_version: int,
    operations: tuple[tuple[str, str, str], ...],
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "apply_next_weekly_plan",
            "arguments": json.dumps(
                {
                    "plan_id": _stable_id(
                        "weekly-plan",
                        TENANT_ID,
                        str(USER_ID),
                        TARGET_WEEK.isoformat(),
                    ),
                    "expected_version": expected_version,
                    "operations": [
                        {
                            "operation_id": f"{call_id}-{index}",
                            "operation": "add",
                            "plan_date": plan_date,
                            "content": content,
                            "source_evidence": {
                                "source_message_index": 1,
                                "exact_clause_quote": exact_quote,
                            },
                        }
                        for index, (plan_date, content, exact_quote) in enumerate(
                            operations,
                            start=1,
                        )
                    ],
                },
                ensure_ascii=False,
            ),
        },
    }


async def _run_turn(
    monkeypatch,
    session: _ConversationSession,
    *,
    source_message_id: str,
    user_text: str,
    now: datetime,
    model_messages: list[dict],
):
    settings = _settings()
    user = SimpleNamespace(
        id=USER_ID,
        name="测试用户",
        timezone="Asia/Shanghai",
        active=True,
        dingtalk_user_id=f"ding-{USER_ID}",
    )

    async def resolve_route(_session, **kwargs):
        capability = ProductionExecutionCapability(
            tenant_id=TENANT_ID,
            user_id=str(USER_ID),
            conversation_id=kwargs["conversation_id"],
            source_message_id=kwargs["source_message_id"],
            control_key="cross-turn-control",
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

    model = _ScriptedModel(model_messages)
    monkeypatch.setattr(
        canary_service,
        "resolve_tool_call_canary_route",
        resolve_route,
    )
    monkeypatch.setattr(canary_service, "ProductionRuntime", _RuntimeFactory)
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
        conversation_id=CONVERSATION_ID,
        source_message_id=source_message_id,
        settings=settings,
        llm_client=SimpleNamespace(native_http_client=model),
        now=now,
        conversation_kind="direct",
        message_occurred_at=now,
    )
    session.remember_turn(
        source_message_id=source_message_id,
        user_text=user_text,
        assistant_text=outcome.message,
        occurred_at=now,
    )
    return outcome, model


def _write_sequence(call: dict, *, reply: str) -> list[dict]:
    reviewed = deepcopy(call)
    reviewed["id"] = f"reviewed-{call['id']}"
    return [
        _assistant_tools(call),
        _assistant_tools(reviewed),
        _terminal(reply=reply, actual_write=True, outcome="changed"),
    ]


def _first_context(model: _ScriptedModel) -> dict:
    return json.loads(model.calls[0]["messages"][1]["content"])[
        "trusted_context"
    ]


def _weekly_items(session: _ConversationSession) -> dict[str, list[str]]:
    plan = session.committed["weekly_plan"]
    if plan is None:
        return {}
    return {
        day.plan_date.isoformat(): [item.original_text for item in day.items]
        for day in plan.days
        if day.items
    }


@pytest.mark.asyncio
async def test_followup_this_moves_one_trusted_plan_item_only_into_today_daily(
    monkeypatch,
) -> None:
    session = _ConversationSession()
    weekly = _weekly_call(
        "weekly-first",
        expected_version=0,
        operations=(("2026-08-17", "日常用印审核", "下周一做日常用印审核"),),
    )
    first, _ = await _run_turn(
        monkeypatch,
        session,
        source_message_id="message-1",
        user_text="下周一做日常用印审核",
        now=NOW,
        model_messages=_write_sequence(weekly, reply="已加入下周一计划。"),
    )
    plan_before = deepcopy(session.committed["weekly_plan"])

    plan = session.committed["weekly_plan"]
    assert plan is not None
    item_ids = tuple(
        item.item_id
        for day in plan.days
        for item in day.items
    )
    daily = _weekly_item_to_daily_call(
        "daily-reference",
        plan_id=plan.plan_id,
        expected_version=plan.version,
        target_item_ids=item_ids,
    )
    second, model = await _run_turn(
        monkeypatch,
        session,
        source_message_id="message-2",
        user_text="今天也做了这个",
        now=NOW + timedelta(minutes=2),
        model_messages=_write_sequence(daily, reply="已记入今天日报。"),
    )

    context = _first_context(model)
    assert first.actual_write is True
    assert second.actual_write is True
    assert session.committed["daily"] == ["日常用印审核"]
    assert session.committed["weekly_plan"] == plan_before
    assert _weekly_items(session) == {"2026-08-17": ["日常用印审核"]}
    assert context["weekly_plan_targets"][0]["version"] == 1
    assert context["recent_messages"][-2]["content"] == "下周一做日常用印审核"


@pytest.mark.asyncio
async def test_followup_this_with_two_trusted_candidates_asks_and_writes_nothing(
    monkeypatch,
) -> None:
    session = _ConversationSession()
    weekly = _weekly_call(
        "weekly-two-candidates",
        expected_version=0,
        operations=(
            ("2026-08-17", "日常用印审核", "下周一做日常用印审核"),
            ("2026-08-18", "合同台账复核", "下周二做合同台账复核"),
        ),
    )
    await _run_turn(
        monkeypatch,
        session,
        source_message_id="message-1",
        user_text="下周一做日常用印审核，下周二做合同台账复核",
        now=NOW,
        model_messages=_write_sequence(weekly, reply="两项已加入下周计划。"),
    )
    before = deepcopy(session.committed)

    outcome, model = await _run_turn(
        monkeypatch,
        session,
        source_message_id="message-2",
        user_text="今天也做了这个",
        now=NOW + timedelta(minutes=2),
        model_messages=[
            {
                "role": "assistant",
                "content": "你说的“这个”是日常用印审核，还是合同台账复核？",
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {"decision": "keep_original"},
                    ensure_ascii=False,
                ),
            },
        ],
    )

    context = _first_context(model)
    assert outcome.actual_write is False
    assert outcome.user_visible_result == "reply_only"
    assert "日常用印审核" in outcome.message
    assert "合同台账复核" in outcome.message
    assert session.committed == before
    assert context["weekly_plan_targets"][0]["version"] == 1
    assert _weekly_items(session) == {
        "2026-08-17": ["日常用印审核"],
        "2026-08-18": ["合同台账复核"],
    }


@pytest.mark.asyncio
async def test_explicit_daily_detour_preserves_weekly_draft_then_resumes_same_plan(
    monkeypatch,
) -> None:
    session = _ConversationSession()
    first_weekly = _weekly_call(
        "weekly-start",
        expected_version=0,
        operations=(("2026-08-17", "日常用印审核", "下周一做日常用印审核"),),
    )
    await _run_turn(
        monkeypatch,
        session,
        source_message_id="message-1",
        user_text="下周一做日常用印审核",
        now=NOW,
        model_messages=_write_sequence(first_weekly, reply="已开始填写下周计划。"),
    )
    plan_after_first = deepcopy(session.committed["weekly_plan"])

    daily = _daily_call("daily-detour", content="完成X")
    daily_outcome, daily_model = await _run_turn(
        monkeypatch,
        session,
        source_message_id="message-2",
        user_text="今天日报：完成X",
        now=NOW + timedelta(minutes=2),
        model_messages=_write_sequence(daily, reply="只更新了今天日报。"),
    )
    plan_after_daily = deepcopy(session.committed["weekly_plan"])

    resumed_weekly = _weekly_call(
        "weekly-resume",
        expected_version=1,
        operations=(
            (
                "2026-08-18",
                "整理合同台账",
                "继续填下周计划：下周二整理合同台账",
            ),
        ),
    )
    resumed_outcome, resumed_model = await _run_turn(
        monkeypatch,
        session,
        source_message_id="message-3",
        user_text="继续填下周计划：下周二整理合同台账",
        now=NOW + timedelta(minutes=4),
        model_messages=_write_sequence(resumed_weekly, reply="已继续补充下周计划。"),
    )

    daily_context = _first_context(daily_model)
    resumed_context = _first_context(resumed_model)
    final_plan = session.committed["weekly_plan"]
    assert daily_outcome.actual_write is True
    assert resumed_outcome.actual_write is True
    assert session.committed["daily"] == ["完成X"]
    assert plan_after_daily == plan_after_first
    assert final_plan.plan_id == plan_after_first.plan_id
    assert final_plan.version == 2
    assert _weekly_items(session) == {
        "2026-08-17": ["日常用印审核"],
        "2026-08-18": ["整理合同台账"],
    }
    assert daily_context["weekly_plan_targets"][0]["version"] == 1
    assert resumed_context["weekly_plan_targets"][0]["plan_id"] == (
        plan_after_first.plan_id
    )
    assert resumed_context["weekly_plan_targets"][0]["version"] == 1
    assert [
        item["content"] for item in resumed_context["recent_messages"] if item["role"] == "user"
    ] == ["下周一做日常用印审核", "今天日报：完成X"]
