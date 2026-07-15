from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert, Update

from app.agent2.business.contracts import (
    BusinessCommandError,
    BusinessCommandContext,
    CreateCaseProgress,
    CreateTravelIntent,
    DeleteCaseProgress,
    ListAssignedCases,
    QueryOperationStatus,
    QueryCaseProgress,
    QueryPartyCases,
    UpdateCaseProgress,
    business_command_fingerprint,
)
from app.agent2.business.models import (
    Agent2Case,
    Agent2OperationOutcome,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseLifecycleState,
    CaseProgress,
    NotificationOutbox,
    TravelCollaborationCandidate,
    PartyCaseClue,
    PartyCaseRole,
    PartyEntity,
    PartyRelation,
)
from app.agent2.business.sql_executor import SqlBusinessExecutor as _SqlBusinessExecutor


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def _owned_case(case_id: UUID, *, owner_user_id: str = "u1") -> Agent2Case:
    return Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id=f"EXT-{case_id}",
        case_number=f"SSGL-{case_id}",
        case_name="测试案件",
        case_type="plaintiff_case",
        status="open",
        owner_user_id=owner_user_id,
        source_type="real_source_sandbox",
        source_id=f"source-{case_id}",
        source_json={},
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def SqlBusinessExecutor(session, **kwargs):  # noqa: N802 - test compatibility factory
    kwargs.setdefault("execution_authority", "authenticated_admin_command")
    return _SqlBusinessExecutor(session, **kwargs)


def test_business_command_fingerprint_normalizes_equivalent_datetime_offsets():
    local = CreateTravelIntent(
        command_id="local-offset",
        destination_raw="南京",
        destination_normalized="南京市",
        city_code="320100",
        province_code="320000",
        start_at=datetime.fromisoformat("2026-07-13T00:00:00+08:00"),
        end_at=datetime.fromisoformat("2026-07-13T23:59:59.999999+08:00"),
        time_precision="day",
        purpose_summary="出差",
        related_case_ids=(),
        confidence=1.0,
    )
    database_round_trip = CreateTravelIntent(
        command_id="database-offset",
        destination_raw="南京",
        destination_normalized="南京市",
        city_code="320100",
        province_code="320000",
        start_at=datetime.fromisoformat("2026-07-12T16:00:00+00:00"),
        end_at=datetime.fromisoformat("2026-07-13T15:59:59.999999+00:00"),
        time_precision="day",
        purpose_summary="出差",
        related_case_ids=(),
        confidence=1.0,
    )

    assert business_command_fingerprint(local) == business_command_fingerprint(
        database_round_trip
    )


def test_case_progress_fingerprint_ignores_replay_time_and_model_scoring_metadata():
    stable = dict(
        command_id="first-model-action",
        case_id=str(uuid4()),
        occurred_at=datetime.fromisoformat("2026-07-12T23:12:20+08:00"),
        progress_type="联系法院",
        summary="今天联系法院，法院表示下周重新查控。",
        details="",
        related_party_ids=(),
        related_document_ids=(),
        related_travel_intent_ids=(),
        confidence=1.0,
    )
    replay = {
        **stable,
        "command_id": "second-model-action",
        "occurred_at": datetime.fromisoformat("2026-07-12T23:12:28+08:00"),
        "progress_type": "法院沟通",
        "confidence": 0.91,
    }

    assert business_command_fingerprint(CreateCaseProgress(**stable)) == business_command_fingerprint(
        CreateCaseProgress(**replay)
    )


class _Nested(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SqlSessionStub:
    def __init__(self, *, visible_case_id: UUID | None, duplicate: bool = False):
        self.visible_case_id = visible_case_id
        self.duplicate = duplicate
        self.added = []
        self.receipt = BusinessCommandReceipt(
            receipt_id=uuid4(),
            tenant_id="tenant-test",
            command_id="cmd-1",
            command_type="create_case_progress",
            actor_user_id="u1",
            source_message_id="msg-1",
            idempotency_key="tenant-test:msg-1:cmd-1",
            status="executed",
            resource_type="case_progress",
            resource_id=str(uuid4()),
            before_json={},
            after_json={"summary": "已开庭"},
            error_code="",
            failed_stage="",
            actual_write=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self._scalar_calls = 0

    async def scalar(self, statement):
        self._scalar_calls += 1
        if isinstance(statement, Insert):
            if self.duplicate:
                return None
            compiled = statement.compile(dialect=postgresql.dialect())
            self.receipt.receipt_id = compiled.params["receipt_id"]
            return self.receipt.receipt_id
        if self.duplicate:
            return self.receipt
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if (
            self.visible_case_id is not None
            and "agent2_cases" in sql
            and len(list(statement.selected_columns)) > 1
        ):
            return _owned_case(self.visible_case_id)
        return self.visible_case_id

    async def execute(self, statement):
        if isinstance(statement, Update):
            params = statement.compile(dialect=postgresql.dialect()).params
            for field in (
                "status",
                "resource_type",
                "resource_id",
                "before_json",
                "after_json",
                "error_code",
                "failed_stage",
                "actual_write",
                "updated_at",
            ):
                if field in params:
                    setattr(self.receipt, field, params[field])
        return None

    async def scalars(self, _statement):
        return type("Rows", (), {"all": lambda _self: []})()

    def add(self, model):
        self.added.append(model)
        if isinstance(model, CaseProgress):
            self.receipt.resource_id = str(model.progress_id)
            self.receipt.after_json = {"summary": model.summary}

    async def flush(self):
        return None

    async def get(self, model, primary_key):
        assert model is BusinessCommandReceipt
        assert primary_key == self.receipt.receipt_id
        return self.receipt

    def begin_nested(self):
        return _Nested()


class _CrossOwnerCreateSession(_SqlSessionStub):
    def __init__(self, case: Agent2Case):
        super().__init__(visible_case_id=case.case_id)
        self.case = case

    async def scalar(self, statement):
        if isinstance(statement, Insert):
            return await super().scalar(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_cases" in sql:
            return self.case
        return await super().scalar(statement)


class _QuerySessionStub:
    def __init__(self, case_id: UUID, rows):
        self.case_id = case_id
        self.rows = rows
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.case_id

    async def scalars(self, statement):
        self.statements.append(statement)
        return type("Rows", (), {"all": lambda _self: list(self.rows)})()


class _CaseInventorySqlSessionStub:
    def __init__(self, case_rows):
        self.case_rows = case_rows
        self.statements = []
        self.added = []
        self.receipt = BusinessCommandReceipt(
            receipt_id=uuid4(),
            tenant_id="tenant-test",
            command_id="case-list-1",
            command_type="list_assigned_cases",
            actor_user_id="u1",
            source_message_id="msg-1",
            idempotency_key="",
            status="processing",
            resource_type="",
            resource_id="",
            before_json={},
            after_json={},
            error_code="",
            failed_stage="",
            actual_write=False,
            created_at=NOW,
            updated_at=NOW,
        )

    async def scalar(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Insert):
            compiled = statement.compile(dialect=postgresql.dialect())
            self.receipt.receipt_id = compiled.params["receipt_id"]
            self.receipt.idempotency_key = compiled.params["idempotency_key"]
            return self.receipt.receipt_id
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Update):
            params = statement.compile(dialect=postgresql.dialect()).params
            for field in (
                "status",
                "resource_type",
                "resource_id",
                "before_json",
                "after_json",
                "error_code",
                "failed_stage",
                "actual_write",
                "updated_at",
            ):
                if field in params:
                    setattr(self.receipt, field, params[field])
            return None
        return type("Rows", (), {"all": lambda _self: list(self.case_rows)})()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def get(self, model, primary_key):
        assert model is BusinessCommandReceipt
        assert primary_key == self.receipt.receipt_id
        return self.receipt

    def begin_nested(self):
        return _Nested()


class _OperationStatusSqlSessionStub(_CaseInventorySqlSessionStub):
    async def scalars(self, statement):
        self.statements.append(statement)
        return type("Rows", (), {"all": lambda _self: list(self.case_rows)})()


class _TravelStatusSqlSessionStub(_CaseInventorySqlSessionStub):
    def __init__(self, candidates, notifications):
        super().__init__(candidates)
        self.notifications = notifications

    async def scalars(self, statement):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        values = (
            self.notifications
            if "agent2_notification_outbox" in sql
            else self.case_rows
        )
        return type("Rows", (), {"all": lambda _self: list(values)})()


class _ProgressWriteSessionStub:
    def __init__(self, progress: CaseProgress):
        self.progress = progress
        self.scalar_calls = 0
        self.refresh_calls = 0

    async def scalar(self, _statement):
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return self.progress
        if self.scalar_calls == 2:
            return self.progress.case_id
        return None

    async def flush(self):
        return None

    async def refresh(self, value):
        assert value is self.progress
        self.refresh_calls += 1


class _PartyQuerySessionStub:
    def __init__(self, party, role_rows, *, relation_rows=(), clue_rows=()):
        self.party = party
        self.role_rows = role_rows
        self.relation_rows = relation_rows
        self.clue_rows = clue_rows
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.party

    async def execute(self, statement):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        values = self.relation_rows if "agent2_party_relations" in sql else self.role_rows
        return type("Rows", (), {"all": lambda _self: list(values)})()

    async def scalars(self, statement):
        self.statements.append(statement)
        return type("Rows", (), {"all": lambda _self: list(self.clue_rows)})()


class _LifecyclePatchSession:
    def __init__(self, state=None):
        self.state = state
        self.added = []

    async def scalar(self, _statement):
        return self.state

    def add(self, value):
        self.added.append(value)
        if isinstance(value, CaseLifecycleState):
            self.state = value


class _LifecycleDeleteSession:
    def __init__(self, progress, lifecycle, receipt):
        self.progress = progress
        self.lifecycle = lifecycle
        self.receipt = receipt
        self.deleted = []
        self.refresh_calls = 0

    async def scalar(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_case_progress" in sql:
            return self.progress
        if "agent2_cases" in sql:
            return self.progress.case_id
        if "agent2_case_lifecycle_states" in sql:
            return self.lifecycle
        if "agent2_business_command_receipts" in sql:
            return self.receipt
        raise AssertionError(sql)

    async def delete(self, value):
        self.deleted.append(value)

    async def flush(self):
        return None

    async def refresh(self, value):
        assert value is self.progress
        self.refresh_calls += 1


def _context(case_id: UUID) -> BusinessCommandContext:
    return BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id="u1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(str(case_id),),
        source_message_id="msg-1",
        source_channel="dingtalk",
        occurred_at=NOW,
    )


def _collaborator_context(case_id: UUID) -> BusinessCommandContext:
    return BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id="u1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(str(case_id),),
        writable_case_ids=(str(case_id),),
        source_message_id="msg-collaborator-1",
        source_channel="dingtalk",
        occurred_at=NOW,
    )


def _command(case_id: UUID) -> CreateCaseProgress:
    return CreateCaseProgress(
        command_id="cmd-1",
        case_id=str(case_id),
        occurred_at=NOW,
        progress_type="hearing",
        summary="已开庭",
        details="法官要求补充证据",
        related_party_ids=(),
        related_document_ids=(),
        related_travel_intent_ids=(),
        confidence=1.0,
    )


def _progress(case_id: UUID) -> CaseProgress:
    return CaseProgress(
        progress_id=uuid4(),
        tenant_id="tenant-test",
        case_id=case_id,
        occurred_at=NOW,
        recorded_at=NOW,
        reporter_id="u1",
        progress_type="court_communication",
        summary="法院预计下周反馈",
        details="",
        source_message_id="msg-create",
        source_channel="dingtalk",
        content_origin="human_record",
        related_party_ids=[],
        related_document_ids=[],
        related_travel_intent_ids=[],
        confidence=1,
        confirmation_status="confirmed_by_reporter",
        version=1,
        idempotency_key="progress-write-fixture",
        deleted_by="",
        delete_reason="",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_lifecycle_patch_persists_stage_node_plan_and_emits_committed_change_facts():
    case_id = uuid4()
    case = Agent2Case(
        case_id=case_id, tenant_id="tenant-test", company_id="company-test",
        department_id="legal", team_id="litigation", external_case_id="P-1",
        case_number="(2026)苏01民初1号", case_name="南京工程款案",
        case_type="plaintiff_case", status="open", owner_user_id="u1",
        source_type="real_source_sandbox", source_id="source-1",
        source_json={"major_stage": "拟诉"}, version=1,
        created_at=NOW, updated_at=NOW,
    )
    progress = _progress(case_id)
    command = CreateCaseProgress(
        **{
            **_command(case_id).__dict__,
            "lifecycle_stage": "诉讼中",
            "lifecycle_node": "已立案",
            "current_status": "法院已受理",
            "next_actions": ("周五联系书记员",),
            "hearing_readiness": "尚未确定开庭日期",
        }
    )
    session = _LifecyclePatchSession()

    snapshot, change, transition = await SqlBusinessExecutor(session)._apply_lifecycle_patch(  # type: ignore[arg-type]
        command=command, context=_context(case_id), case=case, progress=progress,
    )

    assert snapshot["stage"] == "诉讼中"
    assert snapshot["node"] == "已立案"
    assert snapshot["next_actions_json"] == ["周五联系书记员"]
    assert change["from_stage"] == "拟诉"
    assert change["to_stage"] == "诉讼中"
    assert change["case_version"] == 2
    assert case.source_json["major_stage"] == "诉讼中"
    assert case.source_json["minor_stage"] == "已立案"
    assert transition == {"created": True, "before": {}}


@pytest.mark.asyncio
async def test_delete_progress_removes_lifecycle_state_created_by_that_progress():
    case_id = uuid4()
    progress = _progress(case_id)
    lifecycle = CaseLifecycleState(
        lifecycle_state_id=uuid4(), tenant_id="tenant-test", case_id=case_id,
        assigned_user_id="u1", case_type="plaintiff", stage="诉讼中",
        node="已立案", current_status="法院已受理",
        next_actions_json=["周五联系书记员"], hearing_readiness="",
        blocking_issues_json=[], last_progress_id=progress.progress_id,
        version=1, created_at=NOW, updated_at=NOW,
    )
    receipt = BusinessCommandReceipt(
        receipt_id=uuid4(), tenant_id="tenant-test", command_id="create-1",
        command_type="create_case_progress", actor_user_id="u1",
        source_message_id="msg-create", idempotency_key="create-key",
        status="executed", resource_type="case_progress",
        resource_id=str(progress.progress_id), before_json={},
        after_json={
            "lifecycle_state": {"lifecycle_state_id": str(lifecycle.lifecycle_state_id)},
            "lifecycle_transition": {"created": True, "before": {}},
        },
        error_code="", failed_stage="", actual_write=True,
        created_at=NOW, updated_at=NOW,
    )
    session = _LifecycleDeleteSession(progress, lifecycle, receipt)

    deleted = await SqlBusinessExecutor(session)._delete_progress(  # type: ignore[arg-type]
        DeleteCaseProgress(
            command_id="delete-1", progress_id=str(progress.progress_id),
            expected_version=1, reason="用户误记",
        ),
        _context(case_id),
    )

    assert session.deleted == [lifecycle]
    assert deleted.after["deleted_at"] == NOW.isoformat()
    assert deleted.after["lifecycle_reversal"]["action"] == "deleted_created_state"


@pytest.mark.asyncio
async def test_delete_progress_restores_prior_lifecycle_snapshot_with_new_version():
    case_id = uuid4()
    previous_progress_id = uuid4()
    progress = _progress(case_id)
    lifecycle = CaseLifecycleState(
        lifecycle_state_id=uuid4(), tenant_id="tenant-test", case_id=case_id,
        assigned_user_id="u1", case_type="plaintiff", stage="执行中",
        node="提交执行申请", current_status="等待执行立案",
        next_actions_json=["补充财产线索"], hearing_readiness="不适用",
        blocking_issues_json=["暂无财产线索"], last_progress_id=progress.progress_id,
        version=4, created_at=NOW, updated_at=NOW,
    )
    prior = {
        "stage": "诉讼中", "node": "开庭结束", "current_status": "等待判决",
        "next_actions_json": ["跟进判决"], "hearing_readiness": "已开庭",
        "blocking_issues_json": [], "last_progress_id": str(previous_progress_id),
    }
    receipt = BusinessCommandReceipt(
        receipt_id=uuid4(), tenant_id="tenant-test", command_id="create-1",
        command_type="create_case_progress", actor_user_id="u1",
        source_message_id="msg-create", idempotency_key="create-key",
        status="executed", resource_type="case_progress",
        resource_id=str(progress.progress_id), before_json={},
        after_json={"lifecycle_transition": {"created": False, "before": prior}},
        error_code="", failed_stage="", actual_write=True,
        created_at=NOW, updated_at=NOW,
    )
    session = _LifecycleDeleteSession(progress, lifecycle, receipt)

    deleted = await SqlBusinessExecutor(session)._delete_progress(  # type: ignore[arg-type]
        DeleteCaseProgress(
            command_id="delete-2", progress_id=str(progress.progress_id),
            expected_version=1, reason="用户纠正",
        ),
        _context(case_id),
    )

    assert session.deleted == []
    assert lifecycle.stage == "诉讼中"
    assert lifecycle.node == "开庭结束"
    assert lifecycle.next_actions_json == ["跟进判决"]
    assert lifecycle.last_progress_id == previous_progress_id
    assert lifecycle.version == 5
    assert deleted.after["lifecycle_reversal"]["action"] == "restored_prior_state"


@pytest.mark.asyncio
async def test_update_and_delete_refresh_progress_before_receipt_serialization():
    case_id = uuid4()
    progress = _progress(case_id)
    update_session = _ProgressWriteSessionStub(progress)
    updated = await SqlBusinessExecutor(update_session)._update_progress(  # type: ignore[arg-type]
        UpdateCaseProgress(
            command_id="update-1",
            progress_id=str(progress.progress_id),
            expected_version=1,
            summary="法院预计本周五反馈",
            details=None,
        ),
        _context(case_id),
    )

    assert updated.after["version"] == 2
    assert update_session.refresh_calls == 1

    delete_session = _ProgressWriteSessionStub(progress)
    deleted = await SqlBusinessExecutor(delete_session)._delete_progress(  # type: ignore[arg-type]
        DeleteCaseProgress(
            command_id="delete-1",
            progress_id=str(progress.progress_id),
            expected_version=2,
            reason="用户误记",
        ),
        _context(case_id),
    )

    assert deleted.after["version"] == 3
    assert deleted.after["deleted_at"] == NOW.isoformat()
    assert delete_session.refresh_calls == 1


class _RobotFollowupSqlSession(_SqlSessionStub):
    def __init__(
        self,
        *,
        case_id: UUID,
        event: NotificationOutbox,
        additional_events: tuple[NotificationOutbox, ...] = (),
    ):
        super().__init__(visible_case_id=case_id)
        self.event = event
        self.events = (event, *additional_events)
        self.followup_select_sql = ""

    async def scalar(self, statement):
        if isinstance(statement, Insert):
            return await super().scalar(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_notification_outbox" in sql:
            return self.event
        if "agent2_cases" in sql and len(list(statement.selected_columns)) > 1:
            return _owned_case(self.visible_case_id)
        return self.visible_case_id

    async def scalars(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_notification_outbox" in sql:
            self.followup_select_sql = sql
            return type("Rows", (), {"all": lambda _self: list(self.events)})()
        return await super().scalars(statement)


@pytest.mark.asyncio
async def test_robot_followup_reply_is_verified_and_persisted_with_robot_origin():
    case_id = uuid4()
    notification_id = uuid4()
    event = NotificationOutbox(
        notification_id=notification_id,
        tenant_id="tenant-test",
        candidate_id=None,
        recipient_user_id="u1",
        channel="dingtalk",
        message_type="case_progress_followup",
        message_json={
            "case_id": str(case_id),
            "expires_at": datetime(2026, 7, 13, 9, 0, tzinfo=UTC).isoformat(),
        },
        idempotency_key="followup-1",
        status="sent",
        retry_count=0,
        external_message_id="delivery-1",
        response_json={},
        dispatch_history_json=[],
        created_at=NOW,
        updated_at=NOW,
    )
    session = _RobotFollowupSqlSession(case_id=case_id, event=event)
    command = CreateCaseProgress(
        **{
            **_command(case_id).__dict__,
            "followup_notification_id": str(notification_id),
        }
    )

    receipt = await SqlBusinessExecutor(session).execute(  # type: ignore[arg-type]
        command,
        _context(case_id),
    )

    progress = next(item for item in session.added if isinstance(item, CaseProgress))
    assert receipt.status == "executed"
    assert progress.content_origin == "robot_followup"
    assert event.response_json["followup_status"] == "completed"
    assert event.response_json["response_source_message_id"] == "msg-1"
    assert event.response_json["progress_id"] == str(progress.progress_id)
    assert "FOR UPDATE" in session.followup_select_sql


@pytest.mark.asyncio
async def test_robot_followup_reply_blocks_when_multiple_active_tasks_exist():
    case_id = uuid4()

    def event() -> NotificationOutbox:
        return NotificationOutbox(
            notification_id=uuid4(),
            tenant_id="tenant-test",
            candidate_id=None,
            recipient_user_id="u1",
            channel="dingtalk",
            message_type="case_progress_followup",
            message_json={
                "case_id": str(case_id),
                "expires_at": datetime(2026, 7, 13, 9, 0, tzinfo=UTC).isoformat(),
            },
            idempotency_key=str(uuid4()),
            status="sent",
            retry_count=0,
            external_message_id=str(uuid4()),
            response_json={},
            dispatch_history_json=[],
            created_at=NOW,
            updated_at=NOW,
        )

    first = event()
    second = event()
    session = _RobotFollowupSqlSession(
        case_id=case_id,
        event=first,
        additional_events=(second,),
    )
    command = CreateCaseProgress(
        **{
            **_command(case_id).__dict__,
            "followup_notification_id": str(first.notification_id),
        }
    )

    receipt = await SqlBusinessExecutor(session).execute(  # type: ignore[arg-type]
        command,
        _context(case_id),
    )

    assert receipt.status == "blocked"
    assert receipt.error_code == "case_followup_target_needs_clarification"
    assert not any(isinstance(item, CaseProgress) for item in session.added)


@pytest.mark.asyncio
async def test_sql_executor_persists_domain_write_receipt_and_audit_together():
    case_id = uuid4()
    session = _SqlSessionStub(visible_case_id=case_id)

    receipt = await SqlBusinessExecutor(session).execute(_command(case_id), _context(case_id))  # type: ignore[arg-type]

    assert receipt.status == "executed"
    assert receipt.actual_write is True
    assert len([item for item in session.added if isinstance(item, CaseProgress)]) == 1
    assert len([item for item in session.added if isinstance(item, BusinessAuditEvent)]) == 1


@pytest.mark.asyncio
async def test_semantic_sql_executor_blocks_mutation_without_authoritative_ticket():
    case_id = uuid4()
    session = _SqlSessionStub(visible_case_id=case_id)

    receipt = await SqlBusinessExecutor(
        session,
        execution_authority="semantic_ticket",
    ).execute(_command(case_id), _context(case_id))  # type: ignore[arg-type]

    assert receipt.status == "blocked"
    assert receipt.actual_write is False
    assert receipt.error_code == "admission_ticket_required"
    assert not any(isinstance(item, CaseProgress) for item in session.added)


@pytest.mark.asyncio
async def test_sql_executor_duplicate_returns_original_receipt_without_writing_again():
    case_id = uuid4()
    session = _SqlSessionStub(visible_case_id=case_id, duplicate=True)

    receipt = await SqlBusinessExecutor(session).execute(_command(case_id), _context(case_id))  # type: ignore[arg-type]

    assert receipt.status == "duplicate"
    assert receipt.actual_write is False
    assert session.added == []


@pytest.mark.asyncio
async def test_sql_executor_duplicate_blocks_cross_actor_scope_without_leaking_snapshot():
    case_id = uuid4()
    session = _SqlSessionStub(visible_case_id=case_id, duplicate=True)
    session.receipt.actor_user_id = "other-user"
    session.receipt.after_json = {"summary": "sensitive other-user fact"}

    receipt = await SqlBusinessExecutor(session).execute(
        _command(case_id), _context(case_id)
    )  # type: ignore[arg-type]

    assert receipt.status == "blocked"
    assert receipt.error_code == "idempotency_scope_conflict"
    assert receipt.after == {}
    assert receipt.actual_write is False


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", ("blocked", "failed"))
async def test_sql_executor_replay_preserves_prior_failure_fact(prior_status):
    case_id = uuid4()
    session = _SqlSessionStub(visible_case_id=case_id, duplicate=True)
    session.receipt.status = prior_status
    session.receipt.actual_write = False
    session.receipt.error_code = "prior_failure"

    receipt = await SqlBusinessExecutor(session).execute(
        _command(case_id), _context(case_id)
    )  # type: ignore[arg-type]

    assert receipt.status == prior_status
    assert receipt.error_code == "prior_failure"
    assert receipt.actual_write is False


@pytest.mark.asyncio
async def test_sql_executor_replay_reports_processing_as_conflict_not_duplicate():
    case_id = uuid4()
    session = _SqlSessionStub(visible_case_id=case_id, duplicate=True)
    session.receipt.status = "processing"
    session.receipt.actual_write = False

    receipt = await SqlBusinessExecutor(session).execute(
        _command(case_id), _context(case_id)
    )  # type: ignore[arg-type]

    assert receipt.status == "blocked"
    assert receipt.error_code == "idempotency_in_progress"
    assert receipt.actual_write is False


@pytest.mark.asyncio
async def test_sql_executor_audits_successful_read_query_without_claiming_domain_write():
    case_id = uuid4()
    session = _SqlSessionStub(visible_case_id=case_id)
    command = QueryCaseProgress(command_id="query-1", case_id=str(case_id))

    receipt = await SqlBusinessExecutor(session).execute(command, _context(case_id))  # type: ignore[arg-type]

    assert receipt.status == "executed"
    assert receipt.resource_type == "case_progress_query"
    assert receipt.actual_write is False
    audits = [item for item in session.added if isinstance(item, BusinessAuditEvent)]
    assert len(audits) == 1
    assert audits[0].command_type == "query_case_progress"
    assert audits[0].resource_type == "case_progress_query"


@pytest.mark.asyncio
async def test_sql_assigned_case_inventory_is_tenant_actor_and_permission_scoped():
    case_id = uuid4()
    case = Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id="PLAINTIFF-001",
        case_number="（2026）云01民初101号",
        case_name="昆明甲公司合同纠纷案",
        case_type="plaintiff",
        status="open",
        owner_user_id="u1",
        source_type="spreadsheet_import",
        source_id="plaintiff.xlsx:2",
        source_json={},
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    lifecycle = CaseLifecycleState(
        lifecycle_state_id=uuid4(),
        tenant_id="tenant-test",
        case_id=case_id,
        assigned_user_id="u1",
        case_type="plaintiff",
        stage="诉讼中",
        node="已立案",
        current_status="等待开庭",
        next_actions_json=[],
        hearing_readiness="",
        blocking_issues_json=[],
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session = _CaseInventorySqlSessionStub(((case, lifecycle),))

    receipt = await SqlBusinessExecutor(session).execute(  # type: ignore[arg-type]
        ListAssignedCases(command_id="case-list-1"),
        _context(case_id),
    )

    assert receipt.status == "executed"
    assert receipt.actual_write is False
    assert receipt.after["case_count"] == 1
    assert receipt.after["cases"] == [
        {
            "case_id": str(case_id),
            "case_number": "（2026）云01民初101号",
            "case_name": "昆明甲公司合同纠纷案",
            "case_type": "plaintiff",
            "status": "open",
            "stage": "诉讼中",
            "node": "已立案",
        }
    ]
    select_sql = next(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements
        if "agent2_cases" in str(statement.compile(dialect=postgresql.dialect()))
    )
    assert "agent2_cases.tenant_id" in select_sql
    assert "agent2_cases.owner_user_id" in select_sql
    assert "agent2_cases.case_id IN" in select_sql
    audits = [item for item in session.added if isinstance(item, BusinessAuditEvent)]
    assert len(audits) == 1
    assert audits[0].command_type == "list_assigned_cases"


@pytest.mark.asyncio
async def test_sql_operation_status_answers_from_latest_same_conversation_receipts():
    case_id = uuid4()
    previous = Agent2OperationOutcome(
        outcome_id=uuid4(),
        tenant_id="tenant-test",
        user_id="u1",
        conversation_id="conversation-1",
        source_turn_id="previous-turn",
        domain="report",
        operation="create",
        object_type="daily_report",
        object_id="report-1",
        business_status="succeeded",
        message_status="not_applicable",
        actual_write=True,
        would_write=True,
        changed_fields_json=["today_work"],
        user_visible_snapshot_json={"report_type": "daily"},
        blocking_reason="",
        receipt_refs_json=[{"receipt_id": "daily-receipt", "actual_write": True}],
        audit_refs_json=[],
        state_transition_json={"before": "collecting", "after": "collecting"},
        idempotency_key="previous-report-outcome",
        created_at=NOW,
        updated_at=NOW,
    )
    session = _OperationStatusSqlSessionStub((previous,))
    context = BusinessCommandContext(
        **{
            **_context(case_id).as_dict(),
            "source_message_id": "status-question",
            "conversation_id": "conversation-1",
        }
    )

    receipt = await SqlBusinessExecutor(session).execute(  # type: ignore[arg-type]
        QueryOperationStatus(
            command_id="operation-status-1",
            domain="case_progress",
        ),
        context,
    )

    assert receipt.status == "executed"
    assert receipt.actual_write is False
    assert receipt.after["requested_domain"] == "case_progress"
    assert receipt.after["answer"] == "没有。上一条只更新了日报，没有创建案件进展。"
    outcome_sql = next(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements
        if "agent2_operation_outcomes" in str(statement.compile(dialect=postgresql.dialect()))
        and not isinstance(statement, Insert)
    )
    assert "agent2_operation_outcomes.tenant_id" in outcome_sql
    assert "agent2_operation_outcomes.user_id" in outcome_sql
    assert "agent2_operation_outcomes.conversation_id" in outcome_sql
    assert "agent2_operation_outcomes.source_turn_id !=" in outcome_sql


@pytest.mark.asyncio
async def test_sql_travel_collaboration_status_uses_candidate_and_transport_evidence():
    case_id = uuid4()
    candidate_id = uuid4()
    candidate = TravelCollaborationCandidate(
        candidate_id=candidate_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        travel_intent_ids=[str(uuid4()), str(uuid4())],
        participant_ids=["u1", "u2"],
        destination="昆明市",
        overlap_start=NOW,
        overlap_end=NOW,
        match_reason="same_city_and_overlapping_date",
        match_score=Decimal("1.0"),
        status="notified",
        notification_ids=[],
        responses_json={},
        deduplication_key="travel-match-1",
        version=1,
        expires_at=NOW.replace(day=12),
        created_at=NOW,
        updated_at=NOW,
    )
    notification = NotificationOutbox(
        notification_id=uuid4(),
        tenant_id="tenant-test",
        candidate_id=candidate_id,
        recipient_user_id="u2",
        channel="dingtalk",
        message_type="travel_collaboration",
        message_json={},
        idempotency_key="travel-notify-u2",
        status="sent",
        retry_count=0,
        external_message_id="provider-message-1",
        response_json={},
        dispatch_history_json=[],
        error_message="",
        created_at=NOW,
        updated_at=NOW,
    )
    session = _TravelStatusSqlSessionStub((candidate,), (notification,))
    context = BusinessCommandContext(
        **{
            **_context(case_id).as_dict(),
            "source_message_id": "travel-status-question",
            "conversation_id": "conversation-1",
        }
    )

    receipt = await SqlBusinessExecutor(session).execute(  # type: ignore[arg-type]
        QueryOperationStatus(
            command_id="travel-operation-status-1",
            domain="travel",
        ),
        context,
    )

    assert receipt.status == "executed"
    assert receipt.actual_write is False
    assert "昆明市" in receipt.after["answer"]
    assert "同期同地" in receipt.after["answer"]
    assert "钉钉接口已受理" in receipt.after["answer"]
    assert "已收到" not in receipt.after["answer"]
    status_sql = "\n".join(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements
        if not isinstance(statement, (Insert, Update))
    )
    assert "agent2_travel_collaboration_candidates.tenant_id" in status_sql
    assert "agent2_travel_collaboration_candidates.participant_ids" in status_sql
    assert "agent2_notification_outbox.candidate_id" in status_sql


@pytest.mark.asyncio
async def test_sql_executor_blocks_case_outside_permission_scope_before_domain_write():
    allowed_case = uuid4()
    forbidden_case = uuid4()
    session = _SqlSessionStub(visible_case_id=forbidden_case)

    receipt = await SqlBusinessExecutor(session).execute(
        _command(forbidden_case),
        _context(allowed_case),
    )  # type: ignore[arg-type]

    assert receipt.status == "blocked"
    assert receipt.error_code == "case_forbidden"
    assert receipt.actual_write is False
    assert not any(isinstance(item, CaseProgress) for item in session.added)
    audits = [item for item in session.added if isinstance(item, BusinessAuditEvent)]
    assert len(audits) == 1
    assert audits[0].resource_id == ""
    assert audits[0].after_json == {}


@pytest.mark.asyncio
async def test_sql_executor_allows_explicit_collaborator_to_create_progress_for_non_owned_case():
    case_id = uuid4()
    case = Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id="COLLAB-1",
        case_number="SSGL-COLLAB-1",
        case_name="协办案件",
        case_type="plaintiff_case",
        status="open",
        owner_user_id="u2",
        source_type="real_source_sandbox",
        source_id="source-collab-1",
        source_json={},
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session = _CrossOwnerCreateSession(case)

    receipt = await SqlBusinessExecutor(session).execute(
        _command(case_id),
        _collaborator_context(case_id),
    )  # type: ignore[arg-type]

    assert receipt.status == "executed"
    assert receipt.actual_write is True
    progress = next(item for item in session.added if isinstance(item, CaseProgress))
    assert progress.case_id == case_id
    assert progress.reporter_id == "u1"
    assert case.owner_user_id == "u2"


@pytest.mark.asyncio
async def test_sql_executor_keeps_legacy_owner_only_when_collaborator_scope_is_absent():
    case_id = uuid4()
    case = Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id="COLLAB-2",
        case_number="SSGL-COLLAB-2",
        case_name="未授权代录案件",
        case_type="plaintiff_case",
        status="open",
        owner_user_id="u2",
        source_type="real_source_sandbox",
        source_id="source-collab-2",
        source_json={},
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session = _CrossOwnerCreateSession(case)

    receipt = await SqlBusinessExecutor(session).execute(
        _command(case_id),
        _context(case_id),
    )  # type: ignore[arg-type]

    assert receipt.status == "blocked"
    assert receipt.error_code == "case_not_writable"
    assert receipt.actual_write is False
    assert not any(isinstance(item, CaseProgress) for item in session.added)


@pytest.mark.asyncio
async def test_sql_executor_rejects_raw_text_before_touching_the_database():
    case_id = uuid4()
    session = _SqlSessionStub(visible_case_id=case_id)

    with pytest.raises(TypeError, match="typed business command only"):
        await SqlBusinessExecutor(session).execute("记录进展" , _context(case_id))  # type: ignore[arg-type]

    assert session._scalar_calls == 0


@pytest.mark.asyncio
async def test_sql_case_progress_query_returns_rows_and_applies_tenant_case_and_date_scope():
    case_id = uuid4()
    progress = CaseProgress(
        progress_id=uuid4(),
        tenant_id="tenant-test",
        case_id=case_id,
        occurred_at=NOW,
        recorded_at=NOW,
        reporter_id="u1",
        progress_type="hearing",
        summary="已开庭",
        details="",
        source_message_id="msg-create",
        source_channel="dingtalk",
        content_origin="human_record",
        related_party_ids=[],
        related_document_ids=[],
        related_travel_intent_ids=[],
        confidence=1,
        confirmation_status="confirmed_by_reporter",
        version=1,
        idempotency_key="progress-query-fixture",
        deleted_by="",
        delete_reason="",
        created_at=NOW,
        updated_at=NOW,
    )
    session = _QuerySessionStub(case_id, (progress,))
    command = QueryCaseProgress(
        command_id="query-progress",
        case_id=str(case_id),
        start_at=NOW,
        end_at=NOW,
    )

    outcome = await SqlBusinessExecutor(session)._query_progress(  # type: ignore[arg-type]
        command,
        _context(case_id),
    )

    assert outcome.status == "executed"
    assert outcome.actual_write is False
    assert outcome.resource_type == "case_progress_query"
    assert outcome.after["items"][0]["summary"] == "已开庭"
    compiled = session.statements[-1].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "agent2_case_progress.tenant_id" in sql
    assert "agent2_case_progress.case_id" in sql
    assert "agent2_case_progress.deleted_at IS NULL" in sql
    assert "agent2_case_progress.occurred_at >=" in sql
    assert "agent2_case_progress.occurred_at <=" in sql


@pytest.mark.asyncio
async def test_sql_party_query_returns_only_visible_role_sources_not_unscoped_party_source_ids():
    case_id = uuid4()
    party_id = uuid4()
    party = PartyEntity(
        party_id=party_id,
        tenant_id="tenant-test",
        party_type="company",
        canonical_name="南京华东建设有限公司",
        normalized_name="南京华东建设有限公司",
        short_name="华东建设",
        former_names=[],
        unified_social_credit_code="91320100TEST",
        registration_number="",
        legal_representative="张三",
        status="active",
        registered_address="南京市",
        source_type="case_import",
        source_id="forbidden-case-source-id",
        data_quality="confirmed_identifier",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    case = Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id="visible-case",
        case_number="（2026）苏01执1号",
        case_name="华东建设执行案",
        case_type="execution",
        status="open",
        owner_user_id="u1",
        source_type="case_import",
        source_id="visible-case",
        source_json={},
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    role = PartyCaseRole(
        role_id=uuid4(),
        tenant_id="tenant-test",
        party_id=party_id,
        case_id=case_id,
        role_type="defendant",
        source_reference={"source_id": "visible-case:defendant"},
        confirmation_status="confirmed",
        created_at=NOW,
        updated_at=NOW,
    )
    session = _PartyQuerySessionStub(party, ((role, case),))
    command = QueryPartyCases(
        command_id="party-query",
        party_id=str(party_id),
        match_basis="exact_identifier",
        role_type="defendant",
        include_recent_progress=False,
    )

    outcome = await SqlBusinessExecutor(session)._query_party_cases(  # type: ignore[arg-type]
        command,
        _context(case_id),
    )

    assert outcome.status == "executed"
    assert "source_id" not in outcome.after["party"]
    assert outcome.after["source_references"] == [
        {
            "case_id": str(case_id),
            "source_reference": {"source_id": "visible-case:defendant"},
        }
    ]
    assert outcome.after["cases"][0]["case_id"] == str(case_id)


@pytest.mark.asyncio
async def test_sql_party_query_returns_visible_relations_and_typed_business_clues():
    case_id = uuid4()
    party_id = uuid4()
    related_party_id = uuid4()
    party = PartyEntity(
        party_id=party_id,
        tenant_id="tenant-test",
        party_type="company",
        canonical_name="Target Company",
        normalized_name="targetcompany",
        short_name="Target",
        former_names=[],
        unified_social_credit_code="",
        registration_number="",
        legal_representative="",
        status="active",
        registered_address="",
        source_type="case_import",
        source_id="target-source",
        data_quality="confirmed",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    related = PartyEntity(
        party_id=related_party_id,
        tenant_id="tenant-test",
        party_type="person",
        canonical_name="Visible Person",
        normalized_name="visibleperson",
        short_name="",
        former_names=[],
        unified_social_credit_code="",
        registration_number="",
        legal_representative="",
        status="active",
        registered_address="",
        source_type="case_import",
        source_id="person-source",
        data_quality="confirmed",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    case = Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id="visible-case",
        case_number="case-1",
        case_name="Visible Case",
        case_type="execution",
        status="open",
        owner_user_id="u1",
        source_type="case_import",
        source_id="visible-case",
        source_json={},
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    role = PartyCaseRole(
        role_id=uuid4(),
        tenant_id="tenant-test",
        party_id=party_id,
        case_id=case_id,
        role_type="defendant",
        source_reference={"source_id": "visible-case:defendant"},
        confirmation_status="confirmed",
        created_at=NOW,
        updated_at=NOW,
    )
    relation = PartyRelation(
        relation_id=uuid4(),
        tenant_id="tenant-test",
        case_id=case_id,
        from_party_id=party_id,
        to_party_id=related_party_id,
        relation_type="legal_representative",
        source_reference={"case_id": str(case_id), "field": "legal_representative"},
        confirmation_status="confirmed",
        created_at=NOW,
        updated_at=NOW,
    )
    clue = PartyCaseClue(
        clue_id=uuid4(),
        tenant_id="tenant-test",
        case_id=case_id,
        party_id=party_id,
        clue_type="payment",
        label="recovery",
        summary="Recovered payment",
        amount=Decimal("120000.00"),
        currency="CNY",
        occurred_at=NOW,
        source_type="payment_ledger",
        source_id="payment-1",
        source_field="received_amount",
        source_reference={"row": "42"},
        confirmation_status="confirmed",
        created_at=NOW,
        updated_at=NOW,
    )
    session = _PartyQuerySessionStub(
        party,
        ((role, case),),
        relation_rows=((relation, related),),
        clue_rows=(clue,),
    )

    outcome = await SqlBusinessExecutor(session)._query_party_cases(  # type: ignore[arg-type]
        QueryPartyCases(
            command_id="party-query-clues",
            party_id=str(party_id),
            match_basis="exact_canonical_name",
        ),
        _context(case_id),
    )

    assert outcome.after["relations"] == [
        {
            "relation_id": str(relation.relation_id),
            "case_id": str(case_id),
            "relation_type": "legal_representative",
            "direction": "outbound",
            "related_party": {
                "party_id": str(related_party_id),
                "party_type": "person",
                "canonical_name": "Visible Person",
            },
            "source_reference": {"case_id": str(case_id), "field": "legal_representative"},
        }
    ]
    assert outcome.after["business_clues"][0]["clue_type"] == "payment"
    assert outcome.after["business_clues"][0]["amount"] == "120000.00"
    assert outcome.after["clue_counts"]["payment"] == 1
    assert outcome.after["clue_counts"]["asset"] == 0
    sql_statements = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements
    ]
    relation_sql = next(sql for sql in sql_statements if "agent2_party_relations" in sql)
    clue_sql = next(sql for sql in sql_statements if "agent2_party_case_clues" in sql)
    assert "agent2_party_case_roles.case_id IN" in relation_sql
    assert "agent2_party_case_roles.case_id = agent2_party_relations.case_id" in relation_sql
    assert "agent2_party_relations.case_id IN" in relation_sql
    assert "agent2_party_relations.tenant_id" in relation_sql
    assert "agent2_party_case_clues.case_id IN" in clue_sql
    assert "agent2_party_case_clues.party_id" in clue_sql


@pytest.mark.asyncio
async def test_sql_party_query_rejects_fuzzy_candidate_as_an_authoritative_target():
    case_id = uuid4()
    session = _PartyQuerySessionStub(None, ())
    command = QueryPartyCases(
        command_id="party-query-fuzzy",
        party_id=str(uuid4()),
        match_basis="pg_trgm_candidate",
    )

    with pytest.raises(BusinessCommandError) as exc_info:
        await SqlBusinessExecutor(session)._query_party_cases(  # type: ignore[arg-type]
            command,
            _context(case_id),
        )

    assert exc_info.value.code == "party_match_unconfirmed"
    assert session.statements == []
