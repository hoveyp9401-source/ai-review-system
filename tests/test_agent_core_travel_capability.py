from app.agent2.coordination_plan import compile_coordination_plan
from app.agent_core import DailySnapshot, process_agent_turn
from app.agent_core.execution_policy import AuthorizedAction, ExecutionPolicy
from app.agent_core.travel_capability import TravelPlanArtifact, run_travel_capability
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_TRAVEL_COORDINATION


def _envelope(text: str, *, sender_id: str = "user-1", sender_name: str = "庞浩") -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id=sender_id,
        sender_name=sender_name,
        dingtalk_user_id=f"dt-{sender_id}",
        source="unit_test",
        raw_text=text,
        message_id="msg-1",
        conversation_id="conv-1",
    )


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        turn_id="turn-travel",
        plan_id="plan-travel",
        authorized_actions=[
            AuthorizedAction(
                authorization_id="auth-travel",
                plan_id="plan-travel",
                turn_id="turn-travel",
                workflow=WORKFLOW_TRAVEL_COORDINATION,
                capability=WORKFLOW_TRAVEL_COORDINATION,
                operation="upsert_travel_plan",
                write_policy="sandbox",
                reason="test travel grant",
            )
        ],
    )


def test_travel_capability_rejects_candidate_without_authorization():
    envelope = _envelope("明天去南京出差")
    coordination = compile_coordination_plan(envelope)

    result = run_travel_capability(
        turn_id="turn-travel",
        envelope=envelope,
        coordination=coordination,
    )

    assert result.plans == []
    assert result.changed is False
    assert result.read_only is True
    assert result.operation_ledger[0].authorization_status == "denied"
    assert "authorization_denied" in result.operation_ledger[0].safety_flags


def test_process_agent_turn_creates_daily_plan_and_travel_candidate():
    result = process_agent_turn(
        _envelope("明天去南京出差"),
        daily_snapshot=DailySnapshot(),
    )

    # Agent Core operation/replay facts preserve the user's complete direct
    # statement.  Presentation adapters may hide a redundant time prefix, but
    # the auditable business snapshot must retain it.
    assert result.daily_after.tomorrow_plan == ["明天去南京出差"]
    assert result.travel_capability is not None
    assert len(result.travel_capability.plans) == 1
    plan = result.travel_capability.plans[0]
    assert plan.destination == "南京"
    assert plan.date_hint == "tomorrow"
    assert plan.status == "planned"
    assert plan.notification_enabled is False
    assert plan.official_write_enabled is False
    assert [entry.workflow for entry in result.operation_ledger].count(WORKFLOW_TRAVEL_COORDINATION) == 1


def test_process_agent_turn_does_not_treat_travel_product_work_as_trip():
    result = process_agent_turn(
        _envelope("今天做日报系统优化，明天开始做案件进展与出差协同系统"),
        daily_snapshot=DailySnapshot(),
    )

    assert result.travel_capability is None
    assert all(entry.workflow != WORKFLOW_TRAVEL_COORDINATION for entry in result.operation_ledger)


def test_current_trip_candidate_records_return_uncertainty_without_notification():
    result = process_agent_turn(
        _envelope("出差去了南京开庭，同时审核合同"),
        daily_snapshot=DailySnapshot(),
    )

    assert result.travel_capability is not None
    plan = result.travel_capability.plans[0]
    assert plan.destination == "南京"
    assert plan.status == "already_traveled"
    assert plan.needs_return_confirmation is True
    assert result.travel_capability.notification_count == 0
    assert result.travel_capability.official_write_count == 0


def test_travel_capability_detects_same_destination_date_overlap():
    existing = [
        TravelPlanArtifact(
            plan_id="existing-1",
            traveler_id="user-2",
            traveler_name="刘聪",
            destination="南京",
            date_hint="tomorrow",
            status="planned",
            activity_hint="盖章",
        )
    ]

    result = process_agent_turn(
        _envelope("明天去南京开庭"),
        daily_snapshot=DailySnapshot(),
        existing_travel_plans=existing,
    )

    assert result.travel_capability is not None
    assert len(result.travel_capability.overlaps) == 1
    overlap = result.travel_capability.overlaps[0]
    assert overlap.destination == "南京"
    assert overlap.date_hint == "tomorrow"
    assert overlap.plan_ids == [result.travel_capability.plans[0].plan_id, "existing-1"]
    assert "刘聪" in overlap.involved_travelers
