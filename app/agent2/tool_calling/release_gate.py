from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ReleaseTurnAssessment:
    releasable: bool
    blockers: tuple[str, ...]


def assess_release_turn(
    *,
    webhook_status: str,
    observation: Mapping[str, Any],
    accepted_business_results: frozenset[str] = frozenset(
        {"success", "reply_only"}
    ),
) -> ReleaseTurnAssessment:
    """Judge the real Agent2 result, never only the outer webhook state."""

    blockers: list[str] = []
    if str(webhook_status or "") != "processed":
        blockers.append("MESSAGE_NOT_PROCESSED")
    if str(observation.get("message_processing_status") or "") != "consumed":
        blockers.append("AGENT2_NOT_CONSUMED")

    business_result = str(
        observation.get("business_result_status") or "unknown"
    )
    if business_result not in accepted_business_results:
        blockers.append(
            "BUSINESS_RESULT_" + business_result.upper().replace("-", "_")
        )

    if str(observation.get("reply_status") or "") != "formed":
        blockers.append("REPLY_NOT_FORMED")

    model_calls = _nonnegative_int(observation.get("model_call_count"))
    model_attempts = _nonnegative_int(
        observation.get("model_request_attempt_count")
    )
    if not model_calls and not model_attempts:
        blockers.append("MODEL_NOT_CALLED")
    model_result = str(observation.get("model_result_status") or "unknown")
    if model_result != "success":
        blockers.append(
            "MODEL_RESULT_" + model_result.upper().replace("-", "_")
        )

    if _nonnegative_int(observation.get("tool_failure_count")):
        blockers.append("TOOL_FAILURE")
    if _nonnegative_int(observation.get("tool_blocked_count")):
        blockers.append("TOOL_BLOCKED")

    unique_blockers = tuple(dict.fromkeys(blockers))
    return ReleaseTurnAssessment(
        releasable=not unique_blockers,
        blockers=unique_blockers,
    )


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
