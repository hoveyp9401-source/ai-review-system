from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.coordination_sandbox import CANDIDATE_CASE_PROGRESS, CANDIDATE_TRAVEL_COORDINATION
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT, WORKFLOW_MONTHLY_REPORT


def _envelope(raw_text: str, *tasks: ActiveWorkflowTask) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        message_id="msg-1",
        conversation_id="conv-1",
        active_tasks=tuple(tasks),
    )


def test_daily_shadow_builds_command_and_adapter_observation_without_raw_text():
    envelope = _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838")

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.allow_legacy_daily is True
    assert observation["daily_commands"][0]["operation"] == "fill"
    assert observation["legacy_adapter"][0]["legacy_action"] == "append_daily_items"
    assert observation["legacy_adapter"][0]["status"] == "ready"
    assert observation["summary"]["adapter_write_impact_count"] == 1
    assert observation["coordination_plan"]["action_types"] == ["daily_entry"]
    assert observation["summary"]["coordination_action_count"] == 1
    assert "raw_text" not in observation["plan"]
    assert "raw_text" not in observation["coordination_plan"]
    assert "content" not in observation["daily_commands"][0]


def test_daily_shadow_maps_clear_command_to_ready_write():
    envelope = _envelope("\u6e05\u7a7a\u4eca\u65e5\u65e5\u62a5")

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.need_confirmation is False
    assert observation["daily_commands"][0]["operation"] == "clear"
    assert observation["legacy_adapter"][0]["status"] == "ready"
    assert observation["legacy_adapter"][0]["write_impact"] is True
    assert observation["summary"]["adapter_confirmation_count"] == 0


def test_daily_shadow_uses_active_context_for_short_read_request():
    envelope = _envelope(
        "\u53d1\u6211\u770b\u4e0b",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-1",
            status="collecting",
            reply_candidate=True,
        ),
    )

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert observation["daily_commands"][0]["operation"] == "query_current"
    assert observation["legacy_adapter"][0]["read_only"] is True
    assert observation["summary"]["adapter_read_only_count"] == 1


def test_daily_shadow_has_no_daily_command_for_internal_qa():
    envelope = _envelope("\u516c\u53f8\u5370\u7ae0\u501f\u7528\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f")

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.allow_legacy_daily is False
    assert observation["daily_commands"] == []
    assert observation["legacy_adapter"] == []
    assert observation["summary"]["command_count"] == 0


def test_daily_shadow_blocks_short_feedback_with_monthly_and_daily_active_tasks():
    envelope = _envelope(
        "\u5565\u73a9\u610f",
        ActiveWorkflowTask(
            workflow=WORKFLOW_MONTHLY_REPORT,
            task_id="monthly-1",
            status="collecting",
            reply_candidate=False,
        ),
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-1",
            status="collecting",
            reply_candidate=True,
        ),
    )

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.block_legacy_daily is True
    assert observation["daily_commands"] == []
    assert observation["legacy_adapter"] == []
    assert observation["summary"]["command_count"] == 0


def test_daily_shadow_observes_coordination_sandbox_without_side_effects():
    envelope = _envelope("\u660e\u5929\u53ef\u80fd\u51fa\u5dee\u5357\u4eac\u76d6\u7ae0")

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert observation["coordination_sandbox"]["candidate_count"] == 1
    assert observation["coordination_sandbox"]["notification_count"] == 0
    assert observation["coordination_sandbox"]["official_write_count"] == 0
    assert observation["summary"]["sandbox_candidate_count"] == 1
    assert observation["summary"]["sandbox_notification_count"] == 0
    assert observation["summary"]["sandbox_official_write_count"] == 0


def test_daily_shadow_records_pure_future_case_candidate_without_daily_command():
    envelope = _envelope("\u4fdd\u5229\u6848\u4ef6\u4f30\u8ba1\u4e0b\u5468\u8981\u53bb\u5f00\u5ead")

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.block_legacy_daily is True
    assert observation["daily_commands"] == []
    assert observation["coordination_sandbox"]["candidate_count"] == 1
    candidate = observation["coordination_sandbox"]["candidates"][0]
    assert candidate["candidate_type"] == CANDIDATE_CASE_PROGRESS
    assert candidate["target"]["matter_hint"] == "\u4fdd\u5229\u6848\u4ef6"
    assert observation["coordination_sandbox"]["notification_count"] == 0
    assert observation["coordination_sandbox"]["official_write_count"] == 0


def test_daily_shadow_records_future_trip_and_case_candidates_without_daily_command():
    envelope = _envelope("\u4e0b\u5468\u4e94\u53bb\u5357\u4eac\u4e2d\u9662\u6c9f\u901a\u77f3\u5c71\u6848\u8fdb\u5c55")

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.block_legacy_daily is True
    assert observation["daily_commands"] == []
    candidate_types = {
        candidate["candidate_type"]
        for candidate in observation["coordination_sandbox"]["candidates"]
    }
    assert candidate_types == {CANDIDATE_TRAVEL_COORDINATION, CANDIDATE_CASE_PROGRESS}
    assert observation["coordination_sandbox"]["candidate_count"] == 2
    assert observation["coordination_sandbox"]["notification_count"] == 0
    assert observation["coordination_sandbox"]["official_write_count"] == 0


def test_daily_shadow_blocks_day_after_tomorrow_trip_from_legacy_daily_fallback():
    envelope = _envelope(
        "\u540e\u5929\u51fa\u5dee\u5357\u901a\u6c9f\u901a\u4fdd\u5229\u6848\u4ef6\u8c03\u89e3",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-active",
            status="collecting",
            reply_candidate=True,
        ),
    )

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.block_legacy_daily is True
    assert observation["daily_commands"] == []
    assert observation["legacy_adapter"] == []
    candidate_types = {
        candidate["candidate_type"]
        for candidate in observation["coordination_sandbox"]["candidates"]
    }
    assert candidate_types == {CANDIDATE_TRAVEL_COORDINATION, CANDIDATE_CASE_PROGRESS}
    travel = next(
        candidate
        for candidate in observation["coordination_sandbox"]["candidates"]
        if candidate["candidate_type"] == CANDIDATE_TRAVEL_COORDINATION
    )
    case = next(
        candidate
        for candidate in observation["coordination_sandbox"]["candidates"]
        if candidate["candidate_type"] == CANDIDATE_CASE_PROGRESS
    )
    assert travel["target"]["destination"] == "\u5357\u901a"
    assert case["target"]["matter_hint"] == "\u4fdd\u5229\u6848\u4ef6"


def test_daily_shadow_records_tomorrow_trip_without_generic_case_candidate():
    envelope = _envelope("\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u6c9f\u901a\u6848\u4ef6")

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.block_legacy_daily is False
    candidate_types = [
        candidate["candidate_type"]
        for candidate in observation["coordination_sandbox"]["candidates"]
    ]
    assert candidate_types == [CANDIDATE_TRAVEL_COORDINATION]
    candidate = observation["coordination_sandbox"]["candidates"][0]
    assert candidate["target"]["destination"] == "\u4e09\u4e9a"


def test_daily_shadow_records_tomorrow_trip_and_specific_case_candidates():
    envelope = _envelope("\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u529e\u7406\u6d77\u82b1\u5c9b\u6848\u4ef6\u5f00\u5ead")

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")
    observation = evaluation.gate_observation(envelope)

    assert evaluation.gate_decision.block_legacy_daily is False
    candidates = observation["coordination_sandbox"]["candidates"]
    candidate_types = {candidate["candidate_type"] for candidate in candidates}
    assert candidate_types == {CANDIDATE_TRAVEL_COORDINATION, CANDIDATE_CASE_PROGRESS}
    travel = next(candidate for candidate in candidates if candidate["candidate_type"] == CANDIDATE_TRAVEL_COORDINATION)
    case = next(candidate for candidate in candidates if candidate["candidate_type"] == CANDIDATE_CASE_PROGRESS)
    assert travel["target"]["destination"] == "\u4e09\u4e9a"
    assert case["target"]["matter_hint"] == "\u6d77\u82b1\u5c9b\u6848\u4ef6"


def test_daily_shadow_does_not_let_coordination_plan_bypass_no_write_context():
    active_daily = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-active",
        status="collecting",
        reply_candidate=True,
    )
    for raw_text in [
        "\u6211\u60f3\u804a\u4e2a\u6848\u5b50",
        "\u60f3\u8bf4\u4e2a\u6848\u4ef6\u8fdb\u5c55",
        "\u95f2\u804a\u4f1a",
        "\u51fa\u5dee",
        "\u611f\u89c9\u4f60\u53d8\u8822\u4e86\u6709\u70b9",
        "\u88ab\u544a\u7f3a\u5e2d \u539f\u544a\u7f3a\u5e2d\u6709\u4ec0\u4e48\u4e0d\u4e00\u6837\u7684\u540e\u679c",
    ]:
        evaluation = evaluate_daily_shadow(_envelope(raw_text, active_daily), mode="protective_gate")
        observation = evaluation.gate_observation(_envelope(raw_text, active_daily))

        assert observation["coordination_plan"]["action_types"] == [], raw_text
        assert observation["daily_commands"] == [], raw_text
        assert observation["legacy_adapter"] == [], raw_text
