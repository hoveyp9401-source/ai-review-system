from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from app.agent2.coordination_plan import (
    ACTION_APPEND_CASE_PROGRESS,
    ACTION_TRAVEL_EVENT,
    CoordinationAction,
    CoordinationPlan,
)
from app.workflows.intake import WORKFLOW_CASE_PROGRESS, WORKFLOW_TRAVEL_COORDINATION


MODE_OBSERVE_ONLY = "observe_only"
CANDIDATE_TRAVEL_COORDINATION = "travel_coordination_candidate"
CANDIDATE_CASE_PROGRESS = "case_progress_candidate"


@dataclass(frozen=True)
class SandboxCandidate:
    """A candidate captured for later review without business side effects."""

    candidate_id: str
    candidate_type: str
    workflow: str
    source_action_type: str
    target: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    source_text_hash: str = ""
    source_text_chars: int = 0
    confidence: float = 0.0
    status: str = "observed"
    notification_enabled: bool = False
    official_write_enabled: bool = False
    reason: str = ""

    def as_observation(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "workflow": self.workflow,
            "source_action_type": self.source_action_type,
            "target": dict(self.target),
            "payload_keys": sorted(self.payload.keys()),
            "source_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "confidence": self.confidence,
            "status": self.status,
            "notification_enabled": self.notification_enabled,
            "official_write_enabled": self.official_write_enabled,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CoordinationSandboxResult:
    """Observe-only execution sandbox for non-daily coordination actions."""

    mode: str
    candidates: list[SandboxCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    official_write_count: int = 0
    notification_count: int = 0

    def as_observation(self) -> dict[str, Any]:
        candidate_type_counts = Counter(candidate.candidate_type for candidate in self.candidates)
        return {
            "mode": self.mode,
            "candidate_count": len(self.candidates),
            "candidate_type_counts": dict(sorted(candidate_type_counts.items())),
            "official_write_count": self.official_write_count,
            "notification_count": self.notification_count,
            "warnings": list(self.warnings),
            "candidates": [candidate.as_observation() for candidate in self.candidates],
        }


def build_coordination_sandbox(
    plan: CoordinationPlan,
    *,
    mode: str = MODE_OBSERVE_ONLY,
) -> CoordinationSandboxResult:
    """Convert coordination actions into observe-only sandbox candidates."""

    candidates: list[SandboxCandidate] = []
    warnings: list[str] = []
    for action in plan.actions:
        candidate = _candidate_from_action(action)
        if candidate is None:
            continue
        candidates.append(candidate)

    if mode != MODE_OBSERVE_ONLY:
        warnings.append("unsupported_sandbox_mode_forced_observe_only")
    return CoordinationSandboxResult(
        mode=MODE_OBSERVE_ONLY,
        candidates=_dedupe_candidates(candidates),
        warnings=warnings,
        official_write_count=0,
        notification_count=0,
    )


def _candidate_from_action(action: CoordinationAction) -> SandboxCandidate | None:
    if action.action_type == ACTION_TRAVEL_EVENT:
        return SandboxCandidate(
            candidate_id=_candidate_id(CANDIDATE_TRAVEL_COORDINATION, action),
            candidate_type=CANDIDATE_TRAVEL_COORDINATION,
            workflow=WORKFLOW_TRAVEL_COORDINATION,
            source_action_type=action.action_type,
            target=dict(action.target),
            payload={
                "activity_hint": action.payload.get("activity_hint") or "",
                "needs_return_confirmation": bool(action.payload.get("needs_return_confirmation")),
                "content": action.payload.get("content") or "",
            },
            source_text_hash=action.source_text_hash,
            source_text_chars=action.source_text_chars,
            confidence=action.confidence,
            notification_enabled=False,
            official_write_enabled=False,
            reason="observe-only travel coordination candidate; no DingTalk notification is sent",
        )
    if action.action_type == ACTION_APPEND_CASE_PROGRESS:
        return SandboxCandidate(
            candidate_id=_candidate_id(CANDIDATE_CASE_PROGRESS, action),
            candidate_type=CANDIDATE_CASE_PROGRESS,
            workflow=WORKFLOW_CASE_PROGRESS,
            source_action_type=action.action_type,
            target=dict(action.target),
            payload={"content": action.payload.get("content") or ""},
            source_text_hash=action.source_text_hash,
            source_text_chars=action.source_text_chars,
            confidence=action.confidence,
            notification_enabled=False,
            official_write_enabled=False,
            reason="observe-only case progress candidate; no official case record is written",
        )
    return None


def _candidate_id(candidate_type: str, action: CoordinationAction) -> str:
    identity = {
        "candidate_type": candidate_type,
        "source_action_type": action.action_type,
        "target": action.target,
        "source_text_hash": action.source_text_hash,
        "source_segment_index": action.source_segment_index,
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _dedupe_candidates(candidates: list[SandboxCandidate]) -> list[SandboxCandidate]:
    seen: set[str] = set()
    deduped: list[SandboxCandidate] = []
    for candidate in candidates:
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        deduped.append(candidate)
    return deduped
