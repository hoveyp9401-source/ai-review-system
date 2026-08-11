from __future__ import annotations

from app.agent2.tool_calling.release_gate import assess_release_turn


def _observation(**overrides):
    value = {
        "message_processing_status": "consumed",
        "business_result_status": "success",
        "reply_status": "formed",
        "model_call_count": 1,
        "model_request_attempt_count": 1,
        "model_result_status": "success",
        "tool_failure_count": 0,
        "tool_blocked_count": 0,
    }
    value.update(overrides)
    return value


def test_outer_processed_status_cannot_mask_business_failure() -> None:
    result = assess_release_turn(
        webhook_status="processed",
        observation=_observation(business_result_status="failed"),
    )

    assert result.releasable is False
    assert "BUSINESS_RESULT_FAILED" in result.blockers


def test_empty_terminal_reply_and_model_timeout_block_release() -> None:
    empty = assess_release_turn(
        webhook_status="processed",
        observation=_observation(reply_status="missing"),
    )
    timeout = assess_release_turn(
        webhook_status="processed",
        observation=_observation(
            business_result_status="failed",
            model_result_status="failed",
        ),
    )

    assert empty.releasable is False
    assert "REPLY_NOT_FORMED" in empty.blockers
    assert timeout.releasable is False
    assert "MODEL_RESULT_FAILED" in timeout.blockers


def test_tool_conflict_or_incomplete_failure_blocks_release() -> None:
    result = assess_release_turn(
        webhook_status="processed",
        observation=_observation(
            business_result_status="failed",
            tool_failure_count=1,
        ),
    )

    assert result.releasable is False
    assert "TOOL_FAILURE" in result.blockers


def test_expected_clarification_can_pass_but_never_by_default() -> None:
    observation = _observation(business_result_status="clarification")

    default = assess_release_turn(
        webhook_status="processed",
        observation=observation,
    )
    expected = assess_release_turn(
        webhook_status="processed",
        observation=observation,
        accepted_business_results=frozenset({"clarification"}),
    )

    assert default.releasable is False
    assert expected.releasable is True


def test_agent2_release_requires_real_model_evidence() -> None:
    result = assess_release_turn(
        webhook_status="processed",
        observation=_observation(
            model_call_count=0,
            model_request_attempt_count=0,
            model_result_status="not_called",
        ),
    )

    assert result.releasable is False
    assert "MODEL_NOT_CALLED" in result.blockers
