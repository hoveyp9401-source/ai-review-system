from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionRuntimeResult,
)


class _Savepoint:
    def __init__(self, session: "_TransactionalSession") -> None:
        self._session = session
        self._snapshot = deepcopy(session.rows)
        self.is_active = True

    async def commit(self) -> None:
        self.is_active = False
        self._session.savepoint_commit_count += 1

    async def rollback(self) -> None:
        self._session.rows = deepcopy(self._snapshot)
        self.is_active = False
        self._session.savepoint_rollback_count += 1


class _TransactionalSession:
    """Small database-boundary fake with real savepoint semantics for the test."""

    def __init__(self) -> None:
        self.rows = {
            "daily_reports": [{"id": "outer-existing-report"}],
            "weekly_plans": [{"id": "outer-existing-weekly-plan"}],
            "daily_report_audits": [{"id": "outer-existing-audit"}],
            "agent2_tool_call_receipts": [{"id": "outer-existing-receipt"}],
        }
        self.savepoint_commit_count = 0
        self.savepoint_rollback_count = 0

    async def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    def stage_successful_daily_write(self) -> None:
        self.rows["daily_reports"].append({"id": "agent2-new-report"})
        self.rows["daily_report_audits"].append({"id": "agent2-new-audit"})
        self.rows["agent2_tool_call_receipts"].append(
            {"id": "agent2-new-tool-receipt"}
        )

    def stage_successful_daily_and_weekly_write(self) -> None:
        self.stage_successful_daily_write()
        self.rows["weekly_plans"].append({"id": "agent2-new-weekly-plan"})
        self.rows["agent2_tool_call_receipts"].append(
            {"id": "agent2-new-weekly-tool-receipt"}
        )


def _daily_write_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="add_daily_items",
        changed=True,
        target_type="daily_report",
        target_id="agent2-new-report",
        before_version=0,
        after_version=1,
        affected_item_ids=("today-work-1",),
        safe_user_facts={"actual_write": True},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


def _weekly_write_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="apply_next_weekly_plan",
        changed=True,
        target_type="weekly_plan",
        target_id="agent2-new-weekly-plan",
        before_version=0,
        after_version=1,
        affected_item_ids=("weekly-item-1",),
        safe_user_facts={"actual_write": True},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


@pytest.mark.asyncio
async def test_reply_postprocessing_failure_rolls_back_daily_and_weekly_as_one_ingress_turn(
    monkeypatch,
) -> None:
    session = _TransactionalSession()
    baseline = deepcopy(session.rows)
    receipts = (_daily_write_receipt(), _weekly_write_receipt())
    resolution = SimpleNamespace(
        decision=SimpleNamespace(owner="tool_call_core", reason="enabled"),
        control=SimpleNamespace(messages_enabled=True),
        binding=SimpleNamespace(tenant_id="tenant-1"),
        capability=object(),
    )

    async def fake_resolve(*args, **kwargs):
        return resolution

    class FakeContextStore:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeAssembler:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def assemble(self, request):
            return SimpleNamespace(
                namespace=CANARY_STATE_NAMESPACE,
                allowed_tool_names=frozenset(),
            )

    class FakeRuntime:
        def open_session(self, **kwargs):
            return SimpleNamespace(mode=ExecutionMode.CANARY_EXECUTE)

    class FakeAdapter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run_canary_turn(self, **kwargs):
            session.stage_successful_daily_and_weekly_write()
            return SimpleNamespace(
                final_content="日报和下周计划均已更新。",
                receipts=receipts,
                runtime_results=(
                    ProductionRuntimeResult(
                        status="success",
                        receipts=receipts,
                        transaction_opened=True,
                        committed_to_outer_transaction=True,
                        business_write_count=2,
                        receipt_write_count=2,
                    ),
                ),
                model_turns=(),
                request_attempt_count=1,
                transport_retry_count=0,
            )

    monkeypatch.setattr(canary_service, "resolve_tool_call_canary_route", fake_resolve)
    monkeypatch.setattr(canary_service, "ProductionContextStore", FakeContextStore)
    monkeypatch.setattr(canary_service, "TrustedContextAssembler", FakeAssembler)
    monkeypatch.setattr(canary_service, "ProductionRuntime", FakeRuntime)
    monkeypatch.setattr(canary_service, "DeepSeekToolCallingAdapter", FakeAdapter)
    monkeypatch.setattr(
        canary_service,
        "_attach_performance_glossary",
        lambda context, **kwargs: context,
    )
    monkeypatch.setattr(
        canary_service,
        "_select_trusted_read_response",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("reply postprocessing failed")
        ),
    )

    outcome = await canary_service.process_tool_call_canary_ingress(
        session,
        user=SimpleNamespace(
            id="user-1",
            name="测试用户",
            timezone="Asia/Shanghai",
        ),
        dingtalk_user_id="ding-user-1",
        user_text="今天完成材料整理；下周一提交案件材料。",
        source_channel="test",
        conversation_id="conversation-1",
        source_message_id="message-1",
        settings=SimpleNamespace(
            timezone="Asia/Shanghai",
            llm_base_url="https://example.invalid",
        ),
        llm_client=SimpleNamespace(native_http_client=object()),
        now=datetime(2026, 8, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        conversation_kind="direct",
        message_occurred_at=datetime(
            2026,
            8,
            14,
            17,
            59,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )

    assert outcome.owner == "blocked"
    assert outcome.actual_write is False
    assert session.rows == baseline
    assert session.savepoint_commit_count == 0
    assert session.savepoint_rollback_count == 1


@pytest.mark.asyncio
async def test_reply_filter_failure_rolls_back_the_whole_agent2_write_turn(
    monkeypatch,
) -> None:
    session = _TransactionalSession()
    baseline = deepcopy(session.rows)
    receipt = _daily_write_receipt()
    resolution = SimpleNamespace(
        decision=SimpleNamespace(owner="tool_call_core", reason="enabled"),
        control=SimpleNamespace(messages_enabled=True),
        binding=SimpleNamespace(tenant_id="tenant-1"),
        capability=object(),
    )

    async def fake_resolve(*args, **kwargs):
        return resolution

    class FakeContextStore:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeAssembler:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def assemble(self, request):
            return SimpleNamespace(
                namespace=CANARY_STATE_NAMESPACE,
                allowed_tool_names=frozenset(),
            )

    class FakeRuntime:
        def open_session(self, **kwargs):
            return SimpleNamespace(mode=ExecutionMode.CANARY_EXECUTE)

    class FakeAdapter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run_canary_turn(self, **kwargs):
            session.stage_successful_daily_write()
            return SimpleNamespace(
                final_content="日报已更新。",
                receipts=(receipt,),
                runtime_results=(
                    ProductionRuntimeResult(
                        status="success",
                        receipts=(receipt,),
                        transaction_opened=True,
                        committed_to_outer_transaction=True,
                        business_write_count=1,
                        receipt_write_count=1,
                    ),
                ),
                model_turns=(),
                request_attempt_count=1,
                transport_retry_count=0,
            )

    def fail_reply_filter(**kwargs):
        raise RuntimeError("reply filter failed after tool execution")

    monkeypatch.setattr(
        canary_service,
        "resolve_tool_call_canary_route",
        fake_resolve,
    )
    monkeypatch.setattr(canary_service, "ProductionContextStore", FakeContextStore)
    monkeypatch.setattr(canary_service, "TrustedContextAssembler", FakeAssembler)
    monkeypatch.setattr(canary_service, "ProductionRuntime", FakeRuntime)
    monkeypatch.setattr(canary_service, "DeepSeekToolCallingAdapter", FakeAdapter)
    monkeypatch.setattr(
        canary_service,
        "_attach_performance_glossary",
        lambda context, **kwargs: context,
    )
    monkeypatch.setattr(
        canary_service,
        "_select_trusted_read_response",
        fail_reply_filter,
    )

    outcome = await canary_service.process_tool_call_canary_ingress(
        session,
        user=SimpleNamespace(
            id="user-1",
            name="测试用户",
            timezone="Asia/Shanghai",
        ),
        dingtalk_user_id="ding-user-1",
        user_text="今天完成了合同复核",
        source_channel="test",
        conversation_id="conversation-1",
        source_message_id="message-1",
        settings=SimpleNamespace(
            timezone="Asia/Shanghai",
            llm_base_url="https://example.invalid",
        ),
        llm_client=SimpleNamespace(native_http_client=object()),
        now=datetime(2026, 8, 12, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert outcome.owner == "blocked"
    assert outcome.actual_write is False
    assert session.rows == baseline
    assert session.savepoint_commit_count == 0
    assert session.savepoint_rollback_count == 1


@pytest.mark.asyncio
async def test_successful_reply_postprocessing_releases_the_agent2_turn(
    monkeypatch,
) -> None:
    session = _TransactionalSession()
    receipt = _daily_write_receipt()
    resolution = SimpleNamespace(
        decision=SimpleNamespace(owner="tool_call_core", reason="enabled"),
        control=SimpleNamespace(messages_enabled=True),
        binding=SimpleNamespace(tenant_id="tenant-1"),
        capability=object(),
    )

    async def fake_resolve(*args, **kwargs):
        return resolution

    class FakeContextStore:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeAssembler:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def assemble(self, request):
            return SimpleNamespace(
                namespace=CANARY_STATE_NAMESPACE,
                allowed_tool_names=frozenset(),
            )

    class FakeRuntime:
        def open_session(self, **kwargs):
            return SimpleNamespace(mode=ExecutionMode.CANARY_EXECUTE)

    class FakeAdapter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run_canary_turn(self, **kwargs):
            session.stage_successful_daily_write()
            return SimpleNamespace(
                final_content="日报已更新。",
                receipts=(receipt,),
                runtime_results=(
                    ProductionRuntimeResult(
                        status="success",
                        receipts=(receipt,),
                        transaction_opened=True,
                        committed_to_outer_transaction=True,
                        business_write_count=1,
                        receipt_write_count=1,
                    ),
                ),
                model_turns=(),
                request_attempt_count=1,
                transport_retry_count=0,
            )

    monkeypatch.setattr(
        canary_service,
        "resolve_tool_call_canary_route",
        fake_resolve,
    )
    monkeypatch.setattr(canary_service, "ProductionContextStore", FakeContextStore)
    monkeypatch.setattr(canary_service, "TrustedContextAssembler", FakeAssembler)
    monkeypatch.setattr(canary_service, "ProductionRuntime", FakeRuntime)
    monkeypatch.setattr(canary_service, "DeepSeekToolCallingAdapter", FakeAdapter)
    monkeypatch.setattr(
        canary_service,
        "_attach_performance_glossary",
        lambda context, **kwargs: context,
    )
    monkeypatch.setattr(
        canary_service,
        "_select_trusted_read_response",
        lambda **kwargs: "日报已更新。",
    )
    monkeypatch.setattr(
        canary_service,
        "_should_apply_personal_salutation",
        lambda receipts: False,
    )

    outcome = await canary_service.process_tool_call_canary_ingress(
        session,
        user=SimpleNamespace(
            id="user-1",
            name="测试用户",
            timezone="Asia/Shanghai",
        ),
        dingtalk_user_id="ding-user-1",
        user_text="今天完成了合同复核",
        source_channel="test",
        conversation_id="conversation-1",
        source_message_id="message-1",
        settings=SimpleNamespace(
            timezone="Asia/Shanghai",
            llm_base_url="https://example.invalid",
        ),
        llm_client=SimpleNamespace(native_http_client=object()),
        now=datetime(2026, 8, 12, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert outcome.actual_write is True
    assert session.rows["daily_reports"][-1] == {"id": "agent2-new-report"}
    assert session.rows["daily_report_audits"][-1] == {"id": "agent2-new-audit"}
    assert session.rows["agent2_tool_call_receipts"][-1] == {
        "id": "agent2-new-tool-receipt"
    }
    assert session.savepoint_commit_count == 1
    assert session.savepoint_rollback_count == 0
