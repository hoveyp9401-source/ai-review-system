from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.models import CaseProgress, TravelCollaborationCandidate
from app.agent2 import cognitive_runtime_v3 as cognitive_runtime
from app.agent2.cognitive_runtime_v3 import (
    _all_cognitive_commands_succeeded,
    _active_travel_collaboration_resource,
    _daily_history_references,
    _conversation_state_user_key,
    _recent_case_progress_resource,
    _resolve_daily_reference_dates,
)
from app.agent2.typed_daily_executor import build_typed_daily_snapshot
from app.workflows.intake import IncomingMessageEnvelope


def test_phase2_conversation_state_key_is_tenant_user_namespaced_and_actor_checked():
    user = SimpleNamespace(id="user-1")
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-1",
        source="dingtalk_stream",
        raw_text="查询案件",
        conversation_id="conversation-1",
    )
    context = BusinessCommandContext(
        tenant_id="tenant-a",
        company_id="company-a",
        department_id="legal",
        team_id="team-a",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(),
        source_message_id="message-1",
        source_channel="dingtalk_stream",
        occurred_at=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
    )

    assert _conversation_state_user_key(user, envelope, context) == "tenant-a:user-1"
    assert _conversation_state_user_key(user, envelope, None) == "user-1"
    with pytest.raises(ValueError, match="actor"):
        _conversation_state_user_key(
            user,
            envelope,
            BusinessCommandContext(
                **{**context.as_dict(), "actor_user_id": "user-2"}
            ),
        )


@pytest.mark.asyncio
async def test_visible_case_resource_contains_only_permission_scoped_resolution_fields(monkeypatch):
    case_id = str(uuid4())
    context = BusinessCommandContext(
        tenant_id="tenant-a",
        company_id="company-a",
        department_id="legal",
        team_id="team-a",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(case_id,),
        source_message_id="message-1",
        source_channel="dingtalk_stream",
        occurred_at=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
    )

    class _Repository:
        async def list_visible(self, received_context):
            assert received_context is context
            return (
                CaseRecord(
                    case_id=case_id,
                    tenant_id="tenant-a",
                    case_number="（2026）云0102民初1888号",
                    case_name="人民西路8号院物业服务合同纠纷案",
                    party_names=("人民西路8号院业主",),
                    external_case_id="P-018",
                    confirmed_aliases=("人民西路8号院",),
                    version=3,
                ),
            )

    monkeypatch.setattr(
        cognitive_runtime,
        "CaseSqlRepository",
        lambda _session: _Repository(),
    )

    resource = await cognitive_runtime._visible_case_resource(object(), context)

    assert resource == [
        {
            "case_id": case_id,
            "case_number": "（2026）云0102民初1888号",
            "case_name": "人民西路8号院物业服务合同纠纷案",
            "external_case_id": "P-018",
            "confirmed_aliases": ["人民西路8号院"],
            "version": 3,
        }
    ]


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


class _Session:
    def __init__(self, values):
        self.values = values
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _Scalars(self.values)


class _ExecuteResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return _Scalars(self.values)


class _ExecuteSession:
    def __init__(self, values):
        self.values = values
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ExecuteResult(self.values)


def _orchestration_result(*, daily=(), business=(), blocked=()):
    return SimpleNamespace(
        command_plan=SimpleNamespace(
            daily_commands=tuple(daily),
            business_commands=tuple(business),
            blocked_actions=tuple(blocked),
        )
    )


def test_cognitive_state_commit_requires_every_daily_and_business_receipt():
    result = _orchestration_result(
        daily=(SimpleNamespace(command_id="daily-1"),),
        business=(SimpleNamespace(command_id="business-1"),),
    )
    daily_results = [
        {
            "validation_status": "authorized",
            "receipt_id": "daily-receipt-1",
            "typed_command": {"command_id": "daily-1"},
        }
    ]
    business_result = SimpleNamespace(
        actions=(
            SimpleNamespace(
                semantic_command_id="business-1",
                status="executed",
                receipt=SimpleNamespace(receipt_id="business-receipt-1"),
            ),
        )
    )

    assert _all_cognitive_commands_succeeded(
        result,
        command_results=daily_results,
        business_result=business_result,
    ) is True
    assert _all_cognitive_commands_succeeded(
        result,
        command_results=[],
        business_result=business_result,
    ) is False

    assert _all_cognitive_commands_succeeded(
        result,
        command_results=[
            {
                "validation_status": "duplicate",
                "receipt_id": "daily-receipt-1",
                "typed_command": {"command_id": "daily-1"},
            }
        ],
        business_result=business_result,
    ) is True
    assert _all_cognitive_commands_succeeded(
        result,
        command_results=[
            {
                "validation_status": "authorized",
                "receipt_id": "daily-receipt-1",
                "typed_command": {"command_id": "wrong-daily-command"},
            }
        ],
        business_result=business_result,
    ) is False
    assert _all_cognitive_commands_succeeded(
        result,
        command_results=daily_results,
        business_result=SimpleNamespace(
            actions=(
                SimpleNamespace(
                    semantic_command_id="business-1",
                    status="failed",
                    receipt=None,
                ),
            )
        ),
    ) is False


def test_cognitive_state_commit_rejects_planning_blocks_and_partial_business_results():
    blocked = _orchestration_result(
        daily=(SimpleNamespace(command_id="daily-1"),),
        blocked=(object(),),
    )
    assert _all_cognitive_commands_succeeded(
        blocked,
        command_results=[
            {
                "validation_status": "authorized",
                "receipt_id": "daily-receipt-1",
                "typed_command": {"command_id": "daily-1"},
            }
        ],
        business_result=None,
    ) is False


def test_cognitive_state_commit_allows_only_admission_nonwrite_block_with_full_receipt():
    admission_information_block = SimpleNamespace(
        metadata={
            "admission": {
                "status": "information_required",
                "decision_id": "decision-needs-date",
            }
        }
    )
    mixed = _orchestration_result(
        daily=(SimpleNamespace(command_id="daily-1"),),
        blocked=(admission_information_block,),
    )
    committed_daily = [
        {
            "validation_status": "authorized",
            "receipt_id": "daily-receipt-1",
            "typed_command": {"command_id": "daily-1"},
        }
    ]

    assert _all_cognitive_commands_succeeded(
        mixed,
        command_results=committed_daily,
        business_result=None,
    ) is True

    unknown_admission_block = _orchestration_result(
        daily=(SimpleNamespace(command_id="daily-1"),),
        blocked=(
            SimpleNamespace(
                metadata={"admission": {"status": "future_magic_status"}}
            ),
        ),
    )
    assert _all_cognitive_commands_succeeded(
        unknown_admission_block,
        command_results=committed_daily,
        business_result=None,
    ) is False

    partial = _orchestration_result(
        business=(
            SimpleNamespace(command_id="business-1"),
            SimpleNamespace(command_id="business-2"),
        )
    )
    assert _all_cognitive_commands_succeeded(
        partial,
        command_results=[],
        business_result=SimpleNamespace(
            actions=(
                SimpleNamespace(
                    semantic_command_id="business-1",
                    status="duplicate",
                    receipt=SimpleNamespace(receipt_id="business-receipt-1"),
                ),
            )
        ),
    ) is False


def test_action_free_closed_pending_cancellation_can_persist_but_model_like_update_cannot():
    update = SimpleNamespace(
        invalidated_pending_ids=("pending-1",),
        pending_invalidation_reason="cancelled_by_user",
        bind_pending=None,
        consumed_pending_ids=(),
        current_goal="",
        remember_entity_ids=(),
        remember_turn=False,
        user_constraints=None,
        resume_previous_goal=False,
        clear_current_goal=False,
    )
    result = SimpleNamespace(
        command_plan=SimpleNamespace(
            daily_commands=(),
            business_commands=(),
            report_commands=(),
            blocked_actions=(),
        ),
        decision=SimpleNamespace(
            context_update=update,
            required_actions=(),
        ),
        base_state=SimpleNamespace(
            pending=(SimpleNamespace(pending_id="pending-1"),),
        ),
    )

    assert _all_cognitive_commands_succeeded(
        result,
        command_results=[],
        business_result=None,
    ) is True

    unsafe = SimpleNamespace(
        **{
            **result.__dict__,
            "decision": SimpleNamespace(
                context_update=SimpleNamespace(
                    **{
                        **update.__dict__,
                        "current_goal": "model-selected-goal",
                    }
                ),
                required_actions=(),
            ),
        }
    )
    assert _all_cognitive_commands_succeeded(
        unsafe,
        command_results=[],
        business_result=None,
    ) is False


@pytest.mark.asyncio
async def test_recent_case_progress_resource_exposes_only_trusted_permission_filtered_ids_and_versions():
    now = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
    case_id = uuid4()
    progress = CaseProgress(
        progress_id=uuid4(),
        tenant_id="tenant-test",
        case_id=case_id,
        occurred_at=now,
        recorded_at=now,
        reporter_id="user-1",
        progress_type="hearing",
        summary="今天开庭",
        details="",
        source_message_id="message-create",
        source_channel="dingtalk",
        content_origin="human_record",
        related_party_ids=[],
        related_document_ids=[],
        related_travel_intent_ids=[],
        confidence=Decimal("0.99"),
        confirmation_status="confirmed_by_reporter",
        version=3,
        idempotency_key="progress-key",
        deleted_by="",
        delete_reason="",
        created_at=now,
        updated_at=now,
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(str(case_id),),
        source_message_id="message-update",
        source_channel="dingtalk",
        occurred_at=now,
    )
    session = _Session((progress,))

    resource = await _recent_case_progress_resource(session, context)  # type: ignore[arg-type]

    assert resource == [
        {
            "progress_id": str(progress.progress_id),
            "case_id": str(case_id),
            "version": 3,
            "summary": "今天开庭",
            "recorded_at": now.isoformat(),
            "content_origin": "human_record",
            "source_message_id": "message-create",
        }
    ]
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "agent2_case_progress.tenant_id" in sql
    assert "agent2_case_progress.case_id IN" in sql
    assert "agent2_case_progress.reporter_id" in sql
    assert "agent2_case_progress.deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_recent_case_progress_resource_is_empty_without_phase2_business_context():
    session = _Session(())

    assert await _recent_case_progress_resource(session, None) == []  # type: ignore[arg-type]
    assert session.statements == []


@pytest.mark.asyncio
async def test_active_travel_collaboration_resource_is_identity_and_org_scoped():
    now = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
    candidate = TravelCollaborationCandidate(
        candidate_id=uuid4(),
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        travel_intent_ids=[str(uuid4()), str(uuid4())],
        participant_ids=["user-1", "user-2"],
        destination="南京市",
        overlap_start=now,
        overlap_end=now,
        match_reason="same_city_and_overlapping_date",
        match_score=Decimal("0.99"),
        status="notified",
        notification_ids=[str(uuid4()), str(uuid4())],
        responses_json={},
        deduplication_key="candidate-key",
        version=2,
        expires_at=now.replace(hour=23),
        created_at=now,
        updated_at=now,
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(),
        source_message_id="message-response",
        source_channel="dingtalk",
        occurred_at=now,
    )
    session = _Session((candidate,))

    resource = await _active_travel_collaboration_resource(session, context)  # type: ignore[arg-type]

    assert resource[0]["candidate_id"] == str(candidate.candidate_id)
    assert resource[0]["destination"] == "南京市"
    assert resource[0]["participant_ids"] == ["user-1", "user-2"]
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "agent2_travel_collaboration_candidates.tenant_id" in sql
    assert "agent2_travel_collaboration_candidates.company_id" in sql
    assert "agent2_travel_collaboration_candidates.department_id" in sql
    assert "agent2_travel_collaboration_candidates.team_id" in sql
    assert "participant_ids" in sql
    assert "expires_at" in sql


@pytest.mark.asyncio
async def test_daily_history_references_include_persisted_history_and_current_synthetic_snapshot():
    user = SimpleNamespace(id=uuid4(), timezone="Asia/Shanghai")
    previous_date = date(2026, 7, 10)
    previous = SimpleNamespace(
        id=uuid4(),
        user_id=user.id,
        report_date=previous_date,
        updated_at=datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
        status="completed",
        today_work=["previous work"],
        problems=["previous risk"],
        tomorrow_plan=["previous plan"],
        section_status={"_agent2_report_version": 4},
    )
    current_date = date(2026, 7, 11)
    current = build_typed_daily_snapshot(user=user, report_date=current_date, report=None)
    session = _ExecuteSession((previous,))

    references = await _daily_history_references(
        session,
        user=user,
        current_date=current_date,
        current_snapshot=current,
    )

    assert [item.report_date for item in references] == [current_date, previous_date]
    assert references[0].snapshot.report_id == current.report_id
    assert references[1].snapshot.today_work == ("previous work",)
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert "daily_reports.user_id" in str(compiled)
    assert "ORDER BY daily_reports.date DESC" in str(compiled)


def test_daily_reference_dates_resolve_relative_and_arbitrary_explicit_dates():
    assert _resolve_daily_reference_dates(
        "查询昨天和2025-01-03的日报，再看2月4日",
        reference_date=date(2026, 7, 11),
    ) == (
        date(2026, 7, 10),
        date(2025, 1, 3),
        date(2026, 2, 4),
    )
