from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent2.business.composition import BusinessActionResult, BusinessCompositionResult
from app.agent2.business.contracts import BusinessCommandContext, BusinessReceipt
from app.agent2.case_report_projection_runtime import (
    project_committed_case_followup_facts,
)
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)


class _Session:
    def __init__(self, user):
        self.user = user
        self.scalar_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self

    async def get(self, model, key):
        return self.user

    async def scalar(self, statement):
        self.scalar_calls += 1
        return None


class _SessionFactory:
    def __init__(self, user):
        self.user = user

    def __call__(self):
        return _Session(self.user)


class _ForbiddenSessionFactory:
    def __call__(self):
        raise AssertionError("projection flag-off must not open a session")


class _SourceSession:
    async def commit(self):
        _Store.events.append(("source_commit",))


class _ForbiddenSourceSession:
    async def commit(self):
        raise AssertionError("projection flag-off must not commit source session")


class _Store:
    events = []

    def __init__(self, session_factory):
        pass

    async def create_request(self, decision, case_outcome):
        self.events.append(("create", decision.reason_code))
        return "request-1"

    async def mark_succeeded(self, request_id, outcome):
        self.events.append(("succeeded", request_id))

    async def mark_failed(self, request_id, outcome):
        self.events.append(("failed", request_id))


class _DailyExecutor:
    def __init__(self, **kwargs):
        pass

    async def execute_report_projection(self, decision, request_id):
        return OperationOutcome(
            domain="report", operation="create",
            object_ref=OutcomeObjectRef("daily_report", str(uuid4()), "今日日报"),
            business_status="succeeded", message_status="not_applicable",
            changed_fields=(decision.section,),
            user_visible_snapshot={
                "report_type": "daily", "today_work": [decision.normalized_fact],
                "report_item_id": "item-1",
            },
            blocking_reason="",
            receipt_refs=(OutcomeReceiptRef(str(uuid4()), "database", "executed", True),),
            state_transition=OutcomeStateTransition("collecting", "collecting"),
            actual_write=True,
        )


@pytest.mark.asyncio
async def test_projection_flag_off_has_zero_commit_factory_request_or_report():
    outcomes = await project_committed_case_followup_facts(
        business_result=BusinessCompositionResult("message-off", ()),
        business_context=SimpleNamespace(
            tenant_id="tenant-a",
            actor_user_id="user-a",
        ),
        source_session=_ForbiddenSourceSession(),
        session_factory=_ForbiddenSessionFactory(),
        settings=SimpleNamespace(
            case_followup_report_projection_enabled=False,
            case_followup_tenant_allowlist="tenant-a",
            case_followup_user_allowlist="user-a",
        ),
        report_date=date(2026, 7, 14),
    )

    assert outcomes == ()


@pytest.mark.asyncio
async def test_committed_today_action_is_projected_after_case_receipt(monkeypatch):
    import app.agent2.case_report_projection_runtime as runtime

    _Store.events = []
    monkeypatch.setattr(runtime, "SqlReportProjectionStore", _Store)
    monkeypatch.setattr(runtime, "DailyReportProjectionExecutor", _DailyExecutor)
    monkeypatch.setattr(runtime, "get_report", _open_report)
    user_id = uuid4()
    case_id = uuid4()
    progress_id = uuid4()
    receipt_id = uuid4()
    raw = "今天向法院提交了补充材料。"
    receipt = BusinessReceipt(
        receipt_id=str(receipt_id), command_id="command-1",
        command_type="create_case_progress", tenant_id="tenant-a",
        actor_user_id=str(user_id), source_message_id="message-1",
        idempotency_key="case-write-1", status="executed",
        resource_type="case_progress", resource_id=str(progress_id), before={},
        after={
            "case_id": str(case_id), "content_origin": "robot_followup",
            "summary": raw, "version": 1,
        }, error_code=None, failed_stage=None, actual_write=True,
        created_at=SimpleNamespace(),
    )
    action = BusinessActionResult(
        semantic_command_id="semantic-1",
        semantic_command_type="record_case_progress_candidate",
        compiled_command_type="create_case_progress", receipt=receipt,
        outcome_context={
            "case_name": "南京工程款案", "content": raw,
            "case_fact_extraction": {
                "raw_text": raw, "normalized_fact": raw,
                "factual_progress": [], "completed_actions": ["向法院提交了补充材料"],
                "current_status": "", "next_actions": [],
                "action_time_scope": "today", "report_preference": "automatic",
                "confidence": 0.98, "evidence_spans": [[0, len(raw)]],
            },
        },
    )
    result = BusinessCompositionResult("message-1", (action,))
    context = BusinessCommandContext(
        tenant_id="tenant-a", company_id="company", department_id="legal",
        team_id="team", actor_user_id=str(user_id), actor_role_ids=("lawyer",),
        allowed_case_ids=(str(case_id),), source_message_id="message-1",
        source_channel="dingtalk", occurred_at=SimpleNamespace(),
        conversation_id=f"agent2-direct:{user_id}",
    )
    settings = SimpleNamespace(
        case_followup_report_projection_enabled=True,
        case_followup_tenant_allowlist="tenant-a",
        case_followup_user_allowlist=str(user_id),
    )
    user = SimpleNamespace(id=user_id, active=True)

    outcomes = await project_committed_case_followup_facts(
        business_result=result, business_context=context,
        source_session=_SourceSession(),
        session_factory=_SessionFactory(user), settings=settings,
        report_date=date(2026, 7, 13),
    )

    assert len(outcomes) == 1
    assert outcomes[0].business_status == "succeeded"
    assert _Store.events == [
        ("source_commit",),
        ("create", "completed_work_today"),
        ("succeeded", "request-1"),
    ]


@pytest.mark.asyncio
async def test_production_projection_blocks_future_action_mislabeled_as_today(monkeypatch):
    import app.agent2.case_report_projection_runtime as runtime

    _Store.events = []
    monkeypatch.setattr(runtime, "SqlReportProjectionStore", _Store)
    monkeypatch.setattr(runtime, "get_report", _open_report)
    user_id, case_id, progress_id = uuid4(), uuid4(), uuid4()
    raw = "明天与对方律师沟通。"
    receipt = BusinessReceipt(
        receipt_id=str(uuid4()), command_id="command-future",
        command_type="create_case_progress", tenant_id="tenant-a",
        actor_user_id=str(user_id), source_message_id="message-future",
        idempotency_key="case-write-future", status="executed",
        resource_type="case_progress", resource_id=str(progress_id), before={},
        after={
            "case_id": str(case_id), "content_origin": "robot_followup",
            "summary": raw, "version": 1,
        }, error_code=None, failed_stage=None, actual_write=True,
        created_at=SimpleNamespace(),
    )
    action = BusinessActionResult(
        semantic_command_id="semantic-future",
        semantic_command_type="record_case_progress_candidate",
        compiled_command_type="create_case_progress", receipt=receipt,
        outcome_context={
            "case_name": "南京合同案", "content": raw,
            "case_fact_extraction": {
                "raw_text": raw, "normalized_fact": raw,
                "factual_progress": [],
                "completed_actions": ["与对方律师沟通"],
                "current_status": "", "next_actions": [],
                "action_time_scope": "today", "report_preference": "automatic",
                "confidence": 0.99, "evidence_spans": [[0, len(raw)]],
            },
        },
    )
    context = BusinessCommandContext(
        tenant_id="tenant-a", company_id="company", department_id="legal",
        team_id="team", actor_user_id=str(user_id), actor_role_ids=("lawyer",),
        allowed_case_ids=(str(case_id),), source_message_id="message-future",
        source_channel="dingtalk", occurred_at=SimpleNamespace(),
        conversation_id=f"agent2-direct:{user_id}",
    )
    settings = SimpleNamespace(
        case_followup_report_projection_enabled=True,
        case_followup_tenant_allowlist="tenant-a",
        case_followup_user_allowlist=str(user_id),
    )

    outcomes = await project_committed_case_followup_facts(
        business_result=BusinessCompositionResult("message-future", (action,)),
        business_context=context,
        source_session=_SourceSession(),
        session_factory=_SessionFactory(SimpleNamespace(id=user_id, active=True)),
        settings=settings,
        report_date=date(2026, 7, 13),
    )

    assert len(outcomes) == 1
    assert outcomes[0].actual_write is False
    assert outcomes[0].blocking_reason == "action_time_scope_conflict"
    assert _Store.events == [("source_commit",)]


async def _none_report(*args, **kwargs):
    return None


async def _open_report(*args, **kwargs):
    return SimpleNamespace(status="collecting")
