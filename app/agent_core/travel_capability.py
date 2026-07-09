from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any

from app.agent2.coordination_plan import ACTION_TRAVEL_EVENT, CoordinationPlan
from app.agent_core.execution_policy import AuthorizationDecision, ExecutionPolicy, authorize_capability_request
from app.agent_core.types import OperationLedgerEntry
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_TRAVEL_COORDINATION


@dataclass(frozen=True)
class TravelPlanArtifact:
    plan_id: str
    traveler_id: str = ""
    traveler_name: str = ""
    destination: str = ""
    date_hint: str = ""
    status: str = ""
    activity_hint: str = ""
    needs_return_confirmation: bool = False
    source_text_hash: str = ""
    source_text_chars: int = 0
    confidence: float = 0.0
    notification_enabled: bool = False
    official_write_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "traveler_id": self.traveler_id,
            "traveler_name": self.traveler_name,
            "destination": self.destination,
            "date_hint": self.date_hint,
            "status": self.status,
            "activity_hint": self.activity_hint,
            "needs_return_confirmation": self.needs_return_confirmation,
            "source_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "confidence": self.confidence,
            "notification_enabled": self.notification_enabled,
            "official_write_enabled": self.official_write_enabled,
        }


@dataclass(frozen=True)
class TravelOverlap:
    overlap_id: str
    destination: str
    date_hint: str
    plan_ids: list[str] = field(default_factory=list)
    involved_travelers: list[str] = field(default_factory=list)
    reason: str = ""
    notification_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "overlap_id": self.overlap_id,
            "destination": self.destination,
            "date_hint": self.date_hint,
            "plan_ids": list(self.plan_ids),
            "involved_travelers": list(self.involved_travelers),
            "reason": self.reason,
            "notification_enabled": self.notification_enabled,
        }


@dataclass(frozen=True)
class TravelCapabilityResult:
    plans: list[TravelPlanArtifact] = field(default_factory=list)
    overlaps: list[TravelOverlap] = field(default_factory=list)
    operation_ledger: list[OperationLedgerEntry] = field(default_factory=list)
    changed: bool = False
    read_only: bool = False
    notification_count: int = 0
    official_write_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "plans": [plan.as_dict() for plan in self.plans],
            "overlaps": [overlap.as_dict() for overlap in self.overlaps],
            "operation_ledger": [entry.as_dict() for entry in self.operation_ledger],
            "changed": self.changed,
            "read_only": self.read_only,
            "notification_count": self.notification_count,
            "official_write_count": self.official_write_count,
        }


def run_travel_capability(
    *,
    turn_id: str,
    envelope: IncomingMessageEnvelope,
    coordination: CoordinationPlan,
    existing_plans: list[TravelPlanArtifact] | tuple[TravelPlanArtifact, ...] = (),
    execution_policy: ExecutionPolicy | None = None,
) -> TravelCapabilityResult:
    """Create observe-only travel candidates and overlap hints."""

    plans: list[TravelPlanArtifact] = []
    entries: list[OperationLedgerEntry] = []
    read_only = False
    travel_actions = [action for action in coordination.actions if action.action_type == ACTION_TRAVEL_EVENT]
    for index, action in enumerate(travel_actions, start=1):
        decision = authorize_capability_request(
            execution_policy,
            turn_id=turn_id,
            capability=WORKFLOW_TRAVEL_COORDINATION,
            operation="upsert_travel_plan",
            requested_write_policy="sandbox",
        )
        if not decision.allowed:
            read_only = True
            entries.append(
                _operation_entry(
                    turn_id=turn_id,
                    index=index,
                    decision=decision,
                    before_state={},
                    after_state={},
                    read_only=True,
                    safety_flags=decision.safety_flags,
                    reason=decision.reason,
                )
            )
            continue

        plan = _plan_from_action(turn_id, envelope, action, index)
        plans.append(plan)
        entries.append(
            _operation_entry(
                turn_id=turn_id,
                index=index,
                decision=decision,
                before_state={},
                after_state={"plan": plan.as_dict()},
                read_only=False,
                safety_flags=["sandbox", "no_production_write", "no_notification"],
                reason=action.reason or decision.reason,
            )
        )

    overlaps = _detect_overlaps(plans, list(existing_plans or ()))
    return TravelCapabilityResult(
        plans=plans,
        overlaps=overlaps,
        operation_ledger=entries,
        changed=bool(plans),
        read_only=read_only,
        notification_count=0,
        official_write_count=0,
    )


def _plan_from_action(
    turn_id: str,
    envelope: IncomingMessageEnvelope,
    action: Any,
    index: int,
) -> TravelPlanArtifact:
    return TravelPlanArtifact(
        plan_id=_plan_id(turn_id, action, index),
        traveler_id=envelope.sender_id,
        traveler_name=envelope.sender_name,
        destination=str(action.target.get("destination") or "").strip(),
        date_hint=str(action.target.get("date_hint") or "").strip(),
        status=str(action.target.get("status") or "").strip(),
        activity_hint=str(action.payload.get("activity_hint") or "").strip(),
        needs_return_confirmation=bool(action.payload.get("needs_return_confirmation")),
        source_text_hash=action.source_text_hash,
        source_text_chars=action.source_text_chars,
        confidence=float(action.confidence or 0.0),
        notification_enabled=False,
        official_write_enabled=False,
    )


def _detect_overlaps(
    new_plans: list[TravelPlanArtifact],
    existing_plans: list[TravelPlanArtifact],
) -> list[TravelOverlap]:
    overlaps: list[TravelOverlap] = []
    all_previous = list(existing_plans)
    for plan in new_plans:
        for other in all_previous:
            if not _overlaps(plan, other):
                continue
            overlaps.append(
                TravelOverlap(
                    overlap_id=_overlap_id(plan, other),
                    destination=plan.destination,
                    date_hint=plan.date_hint,
                    plan_ids=[plan.plan_id, other.plan_id],
                    involved_travelers=_travelers(plan, other),
                    reason="same destination and date hint in travel sandbox",
                    notification_enabled=False,
                )
            )
        all_previous.append(plan)
    return overlaps


def _overlaps(left: TravelPlanArtifact, right: TravelPlanArtifact) -> bool:
    if left.plan_id == right.plan_id:
        return False
    if left.traveler_id and right.traveler_id and left.traveler_id == right.traveler_id:
        return False
    if not left.destination or not right.destination:
        return False
    if _compact(left.destination) != _compact(right.destination):
        return False
    if not left.date_hint or not right.date_hint:
        return False
    return left.date_hint == right.date_hint


def _travelers(left: TravelPlanArtifact, right: TravelPlanArtifact) -> list[str]:
    names = [left.traveler_name or left.traveler_id, right.traveler_name or right.traveler_id]
    return [name for index, name in enumerate(names) if name and name not in names[:index]]


def _operation_entry(
    *,
    turn_id: str,
    index: int,
    decision: AuthorizationDecision,
    before_state: dict[str, Any],
    after_state: dict[str, Any],
    read_only: bool,
    safety_flags: list[str],
    reason: str,
) -> OperationLedgerEntry:
    return OperationLedgerEntry(
        operation_id=_operation_id(turn_id, index),
        workflow=WORKFLOW_TRAVEL_COORDINATION,
        capability=WORKFLOW_TRAVEL_COORDINATION,
        operation="upsert_travel_plan",
        write_policy=decision.write_policy if decision.allowed else "blocked",
        plan_id=decision.plan_id,
        authorization_id=decision.authorization_id,
        authorization_status="allowed" if decision.allowed else "denied",
        before_state=before_state,
        after_state=after_state,
        changed=False,
        read_only=read_only,
        safety_flags=list(safety_flags),
        reason=reason,
    )


def _operation_id(turn_id: str, index: int) -> str:
    return hashlib.sha256(f"{turn_id}:{WORKFLOW_TRAVEL_COORDINATION}:upsert_travel_plan:{index}".encode("utf-8")).hexdigest()[:16]


def _plan_id(turn_id: str, action: Any, index: int) -> str:
    raw = f"{turn_id}:{index}:{action.source_text_hash}:{action.target}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _overlap_id(left: TravelPlanArtifact, right: TravelPlanArtifact) -> str:
    raw = "|".join(sorted([left.plan_id, right.plan_id]))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _compact(value: str) -> str:
    return "".join(str(value or "").split())
