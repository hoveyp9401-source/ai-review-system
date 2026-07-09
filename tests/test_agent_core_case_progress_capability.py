from app.agent2.coordination_plan import compile_coordination_plan
from app.agent_core import DailySnapshot, process_agent_turn
from app.agent_core.case_progress_capability import CaseRecord, run_case_progress_capability
from app.agent_core.execution_policy import AuthorizedAction, ExecutionPolicy
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_CASE_PROGRESS, WORKFLOW_INTERNAL_QA


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
        turn_id="turn-case",
        plan_id="plan-case",
        authorized_actions=[
            AuthorizedAction(
                authorization_id="auth-case",
                plan_id="plan-case",
                turn_id="turn-case",
                workflow=WORKFLOW_CASE_PROGRESS,
                capability=WORKFLOW_CASE_PROGRESS,
                operation="append_case_progress",
                write_policy="sandbox",
                reason="test case progress grant",
            )
        ],
    )


def test_case_progress_capability_rejects_candidate_without_authorization():
    envelope = _envelope("恒大破产案沟通，补充诉讼材料")
    coordination = compile_coordination_plan(envelope)

    result = run_case_progress_capability(
        turn_id="turn-case",
        envelope=envelope,
        coordination=coordination,
    )

    assert result.items == []
    assert result.changed is False
    assert result.read_only is True
    assert result.operation_ledger[0].authorization_status == "denied"
    assert "authorization_denied" in result.operation_ledger[0].safety_flags


def test_process_agent_turn_creates_daily_entry_and_case_progress_candidate():
    result = process_agent_turn(
        _envelope("恒大破产案沟通，补充诉讼材料"),
        daily_snapshot=DailySnapshot(),
    )

    assert result.case_progress_capability is not None
    assert len(result.case_progress_capability.items) == 1
    item = result.case_progress_capability.items[0]
    assert item.matter_hint == "恒大破产案"
    assert item.reporter_name == "庞浩"
    assert item.official_write_enabled is False
    assert item.notification_enabled is False
    assert any(entry.workflow == WORKFLOW_CASE_PROGRESS for entry in result.operation_ledger)


def test_case_progress_links_known_case_record_by_matter_hint():
    result = process_agent_turn(
        _envelope("苏建院借章事项今天和法院沟通了执行进展"),
        daily_snapshot=DailySnapshot(),
        case_records=[
            CaseRecord(
                case_id="case-1",
                matter_hint="苏建院借章事项",
                owner_id="owner-1",
                owner_name="陆健",
            )
        ],
    )

    assert result.case_progress_capability is not None
    item = result.case_progress_capability.items[0]
    assert item.case_id == "case-1"
    assert item.owner_name == "陆健"


def test_generic_case_work_does_not_create_case_progress_candidate():
    result = process_agent_turn(
        _envelope("项目评审、案件沟通"),
        daily_snapshot=DailySnapshot(),
    )

    assert result.case_progress_capability is None
    assert all(entry.workflow != WORKFLOW_CASE_PROGRESS for entry in result.operation_ledger)


def test_legal_question_does_not_become_case_progress_candidate():
    result = process_agent_turn(
        _envelope("被告缺席和原告缺席有什么区别？"),
        daily_snapshot=DailySnapshot(),
    )

    assert result.routing.primary_workflow == WORKFLOW_INTERNAL_QA
    assert result.case_progress_capability is None
