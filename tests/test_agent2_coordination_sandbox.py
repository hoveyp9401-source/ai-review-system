from app.agent2.coordination_plan import compile_coordination_plan
from app.agent2.coordination_sandbox import (
    CANDIDATE_CASE_PROGRESS,
    CANDIDATE_TRAVEL_COORDINATION,
    build_coordination_sandbox,
)
from app.workflows.intake import IncomingMessageEnvelope


def _envelope(raw_text: str) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
    )


def test_sandbox_creates_travel_candidate_without_notification_or_write():
    plan = compile_coordination_plan(_envelope("明天可能出差南京盖章"))

    sandbox = build_coordination_sandbox(plan)

    assert sandbox.mode == "observe_only"
    assert sandbox.notification_count == 0
    assert sandbox.official_write_count == 0
    assert [candidate.candidate_type for candidate in sandbox.candidates] == [CANDIDATE_TRAVEL_COORDINATION]
    candidate = sandbox.candidates[0]
    assert candidate.target["destination"] == "南京"
    assert candidate.target["date_hint"] == "tomorrow"
    assert candidate.notification_enabled is False
    assert candidate.official_write_enabled is False


def test_sandbox_creates_case_candidate_without_official_case_write():
    plan = compile_coordination_plan(_envelope("恒大破产案沟通，补充诉讼材料"))

    sandbox = build_coordination_sandbox(plan)

    assert [candidate.candidate_type for candidate in sandbox.candidates] == [CANDIDATE_CASE_PROGRESS]
    candidate = sandbox.candidates[0]
    assert candidate.target["matter_hint"] == "恒大破产案"
    assert candidate.notification_enabled is False
    assert candidate.official_write_enabled is False


def test_sandbox_ignores_daily_only_actions():
    plan = compile_coordination_plan(_envelope("今天完成合同审核，没啥问题"))

    sandbox = build_coordination_sandbox(plan)

    assert sandbox.candidates == []
    assert sandbox.notification_count == 0
    assert sandbox.official_write_count == 0


def test_sandbox_observation_does_not_leak_raw_text_or_payload_content():
    plan = compile_coordination_plan(_envelope("出差去了南京开庭，同时审核合同"))
    sandbox = build_coordination_sandbox(plan)

    observation = sandbox.as_observation()

    assert "raw_text" not in observation
    assert observation["candidate_count"] == 1
    assert observation["candidate_type_counts"] == {CANDIDATE_TRAVEL_COORDINATION: 1}
    candidate = observation["candidates"][0]
    assert candidate["candidate_id"]
    assert "raw_text" not in candidate
    assert "content" not in candidate
    assert "content" in candidate["payload_keys"]


def test_sandbox_keeps_travel_and_case_candidates_independent():
    plan = compile_coordination_plan(
        _envelope("\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u529e\u7406\u6d77\u82b1\u5c9b\u6848\u4ef6\u5f00\u5ead")
    )

    sandbox = build_coordination_sandbox(plan)

    assert sandbox.mode == "observe_only"
    assert sandbox.notification_count == 0
    assert sandbox.official_write_count == 0
    assert {candidate.candidate_type for candidate in sandbox.candidates} == {
        CANDIDATE_TRAVEL_COORDINATION,
        CANDIDATE_CASE_PROGRESS,
    }
    travel = next(candidate for candidate in sandbox.candidates if candidate.candidate_type == CANDIDATE_TRAVEL_COORDINATION)
    case = next(candidate for candidate in sandbox.candidates if candidate.candidate_type == CANDIDATE_CASE_PROGRESS)
    assert travel.workflow != case.workflow
    assert travel.target["destination"] == "\u4e09\u4e9a"
    assert case.target["matter_hint"] == "\u6d77\u82b1\u5c9b\u6848\u4ef6"
