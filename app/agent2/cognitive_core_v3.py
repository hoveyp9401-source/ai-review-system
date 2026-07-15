from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import hashlib
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from app.agent2.admission_contracts import (
    AdmissionMode,
    AdmissionTicket,
    AdmissionTrace,
    DeferredSemanticEvent,
    InformationPending,
    SemanticReviewItem,
    TrustedSelectionRequest,
)
from app.agent2.conversation_state import (
    BoundPending,
    ConversationEntity,
    ConversationGoal,
    ConversationState,
    RecentContextFrame,
    UserConstraints,
)


COGNITIVE_CORE_CONTRACT_VERSION = "cognitive_core.v3"


class SemanticInputLimitExceeded(ValueError):
    """Raised before model invocation when a turn exceeds the reviewed contract."""


_FORBIDDEN_DECISION_KEYS = {
    "allow_write",
    "should_write_db",
    "effects",
    "commands",
    "database_operation",
}
_ACTION_ENTITY_TYPES = {
    "capture_daily_event": ("daily_event", 1, 1),
    "edit_daily_item": ("daily_item_target", 1, 1),
    "delete_daily_item": ("daily_item_target", 1, 1),
    "merge_daily_items": ("daily_item_target", 1, 1),
    "query_daily_report": ("daily_report", 1, 1),
    "copy_previous_daily_report": ("daily_report", 1, 1),
    "clear_daily_section": ("daily_report", 1, 1),
    "clear_daily_report": ("daily_report", 1, 1),
    "reopen_daily_report": ("daily_report", 1, 1),
    "copy_current_work_to_tomorrow": ("daily_report", 1, 1),
    "complete_previous_daily_plan": ("daily_report", 1, 1),
    "record_case_progress": ("case_ref", 1, 1),
    "update_case_progress": ("case_progress_ref", 1, 1),
    "delete_case_progress": ("case_progress_ref", 1, 1),
    "query_case_progress": ("case_progress_ref", 1, 1),
    "query_operation_status": ("operation_status_query", 1, 1),
    "link_case_progress": ("case_progress_ref", 1, 1),
    "answer_case_query": ("case_query", 1, 1),
    "record_travel_event": ("travel_event", 1, 1),
    "respond_travel_collaboration": ("travel_collaboration_ref", 1, 1),
    "search_enterprise_knowledge": ("knowledge_query", 1, 1),
    "capture_report_event": ("report_event", 1, 1),
    "query_periodic_report": ("periodic_report", 1, 1),
    "submit_periodic_report": ("periodic_report", 1, 1),
    "edit_periodic_report_item": ("report_item_target", 1, 1),
    "delete_periodic_report_item": ("report_item_target", 1, 1),
    "update_case_followup_policy": ("case_followup_policy", 1, 1),
    "trigger_case_followup_now": ("case_followup_policy", 1, 1),
}


@dataclass(frozen=True)
class CognitiveTurn:
    user_id: str
    conversation_id: str
    message_id: str
    text: str
    occurred_at: datetime
    resources: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = ""
    actor_user_id: str = ""


@dataclass(frozen=True)
class RequiredAction:
    action_id: str
    action_type: str
    intent: str
    entity_ids: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClarificationNeed:
    reason: str
    missing_fields: tuple[str, ...]
    question: str


@dataclass(frozen=True)
class SemanticContextUpdate:
    current_goal: str = ""
    preserve_current_goal: bool = False
    remember_entity_ids: tuple[str, ...] = ()
    remember_turn: bool = False
    bind_pending: "PendingBindingRequest | None" = None
    user_constraints: "UserConstraintUpdate | None" = None
    consumed_pending_ids: tuple[str, ...] = ()
    resume_previous_goal: bool = False
    clear_current_goal: bool = False


@dataclass(frozen=True)
class PendingBindingRequest:
    pending_id: str
    intent: str
    action: str
    entity_ids: tuple[str, ...]
    expires_in_seconds: int


@dataclass(frozen=True)
class UserConstraintUpdate:
    no_daily_write: bool | None = None
    read_only: bool | None = None
    no_history_mutation: bool | None = None
    draft_only: bool | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticSegment:
    segment_id: str
    text: str
    text_hash: str
    intents: tuple[str, ...]
    entity_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    start_offset: int = -1
    end_offset: int = -1


@dataclass(frozen=True)
class SemanticInterpretation:
    intents: tuple[str, ...]
    segments: tuple[SemanticSegment, ...]
    entities: tuple[ConversationEntity, ...]
    confidence: float
    required_actions: tuple[RequiredAction, ...]
    clarification_need: ClarificationNeed | None
    context_update: SemanticContextUpdate

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SemanticInterpretation":
        if not isinstance(payload, dict):
            raise ValueError("semantic interpretation must be an object")
        forbidden = _FORBIDDEN_DECISION_KEYS.intersection(payload)
        if forbidden:
            raise ValueError(f"cognitive decision cannot contain execution fields: {sorted(forbidden)}")
        intents = tuple(str(value).strip() for value in payload.get("intents", []) if str(value).strip())
        if not intents:
            raise ValueError("semantic interpretation requires at least one intent")
        confidence = float(payload.get("confidence", 0))
        if not 0 <= confidence <= 1:
            raise ValueError("semantic confidence must be between 0 and 1")
        entities = tuple(_entity_from_payload(value) for value in payload.get("entities", []))
        entity_ids = {entity.entity_id for entity in entities}
        actions = tuple(_action_from_payload(value) for value in payload.get("required_actions", []))
        action_ids = {action.action_id for action in actions}
        for action in actions:
            missing = set(action.entity_ids) - entity_ids
            if missing:
                raise ValueError(f"required action references unknown entities: {sorted(missing)}")
            expected_entity = _ACTION_ENTITY_TYPES.get(action.action_type)
            if expected_entity is not None:
                entity_type, minimum, maximum = expected_entity
                referenced = [entity for entity in entities if entity.entity_id in action.entity_ids]
                if not minimum <= len(referenced) <= maximum or any(
                    entity.entity_type != entity_type for entity in referenced
                ):
                    raise ValueError(
                        f"{action.action_type} requires {entity_type} entity binding"
                    )
                if action.action_type == "capture_daily_event" and str(
                    referenced[0].attributes.get("field") or ""
                ) not in {"today_work", "problems", "tomorrow_plan"}:
                    raise ValueError("capture_daily_event requires daily_event.attributes.field")
                if action.action_type == "capture_report_event":
                    attributes = referenced[0].attributes
                    if attributes.get("report_type") not in {"weekly", "monthly"}:
                        raise ValueError("capture_report_event requires a periodic report_type")
                    if attributes.get("field") not in {
                        "accomplishments",
                        "risks",
                        "next_plan",
                        "metrics",
                    }:
                        raise ValueError("capture_report_event requires a valid report field")
        segments = tuple(_segment_from_payload(value) for value in payload.get("segments", []))
        if len({segment.segment_id for segment in segments}) != len(segments):
            raise ValueError("semantic interpretation contains duplicate segment ids")
        for segment in segments:
            if set(segment.intents) - set(intents):
                raise ValueError("semantic segment references an unknown turn intent")
            if set(segment.entity_ids) - entity_ids:
                raise ValueError("semantic segment references unknown entities")
            if set(segment.action_ids) - action_ids:
                raise ValueError("semantic segment references unknown actions")
        clarification_raw = payload.get("clarification_need")
        clarification = _clarification_from_payload(clarification_raw) if clarification_raw else None
        update_raw = payload.get("context_update") or {}
        if not isinstance(update_raw, dict):
            raise ValueError("context_update must be an object")
        remembered_ids = tuple(
            str(value).strip() for value in update_raw.get("remember_entity_ids", []) if str(value).strip()
        )
        if set(remembered_ids) - entity_ids:
            raise ValueError("context_update can only remember entities from this decision")
        pending_request = _pending_request_from_payload(update_raw.get("bind_pending"))
        if pending_request is not None and set(pending_request.entity_ids) - entity_ids:
            raise ValueError("pending can only bind entities from this decision")
        constraint_update = _constraint_update_from_payload(update_raw.get("user_constraints"))
        return cls(
            intents=intents,
            segments=segments,
            entities=entities,
            confidence=confidence,
            required_actions=actions,
            clarification_need=clarification,
            context_update=SemanticContextUpdate(
                current_goal=str(update_raw.get("current_goal") or "").strip(),
                preserve_current_goal=bool(update_raw.get("preserve_current_goal", False)),
                remember_entity_ids=remembered_ids,
                remember_turn=bool(update_raw.get("remember_turn", False)),
                bind_pending=pending_request,
                user_constraints=constraint_update,
                resume_previous_goal=bool(update_raw.get("resume_previous_goal", False)),
                clear_current_goal=bool(update_raw.get("clear_current_goal", False)),
            ),
        )


@dataclass(frozen=True)
class CognitiveDecisionV3:
    decision_id: str
    intents: tuple[str, ...]
    segments: tuple[SemanticSegment, ...]
    entities: tuple[ConversationEntity, ...]
    confidence: float
    required_actions: tuple[RequiredAction, ...]
    clarification_need: ClarificationNeed | None
    context_update: SemanticContextUpdate
    source_text_hash: str
    contract_version: str = COGNITIVE_CORE_CONTRACT_VERSION
    admission_tickets: tuple[AdmissionTicket, ...] = ()
    admission_information_pendings: tuple[InformationPending, ...] = ()
    admission_selection_requests: tuple[TrustedSelectionRequest, ...] = ()
    admission_review_items: tuple[SemanticReviewItem, ...] = ()
    admission_deferred_events: tuple[DeferredSemanticEvent, ...] = ()
    admission_trace: AdmissionTrace | None = None
    admission_mode: AdmissionMode = "disabled"

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "decision_id": self.decision_id,
            "intents": list(self.intents),
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "text": segment.text,
                    "text_hash": segment.text_hash,
                    "intents": list(segment.intents),
                    "entity_ids": list(segment.entity_ids),
                    "action_ids": list(segment.action_ids),
                    "start_offset": segment.start_offset,
                    "end_offset": segment.end_offset,
                }
                for segment in self.segments
            ],
            "entities": [
                {
                    "entity_id": entity.entity_id,
                    "entity_type": entity.entity_type,
                    "value": entity.value,
                    "confidence": entity.confidence,
                    "attributes": dict(entity.attributes),
                    "source_context_id": entity.source_context_id,
                }
                for entity in self.entities
            ],
            "confidence": self.confidence,
            "required_actions": [
                {
                    "action_id": action.action_id,
                    "action_type": action.action_type,
                    "intent": action.intent,
                    "entity_ids": list(action.entity_ids),
                    "parameters": dict(action.parameters),
                }
                for action in self.required_actions
            ],
            "clarification_need": (
                {
                    "reason": self.clarification_need.reason,
                    "missing_fields": list(self.clarification_need.missing_fields),
                    "question": self.clarification_need.question,
                }
                if self.clarification_need is not None
                else None
            ),
            "context_update": _context_update_payload(self.context_update),
            "source_text_hash": self.source_text_hash,
            "admission_tickets": [ticket.as_dict() for ticket in self.admission_tickets],
            "admission_information_pendings": [
                pending.as_dict()
                for pending in self.admission_information_pendings
            ],
            "admission_selection_requests": [
                request.as_dict() for request in self.admission_selection_requests
            ],
            "admission_review_items": [
                item.as_dict() for item in self.admission_review_items
            ],
            "admission_deferred_events": [
                event.as_dict() for event in self.admission_deferred_events
            ],
            "admission_trace": (
                self.admission_trace.as_dict() if self.admission_trace is not None else None
            ),
            "admission_mode": self.admission_mode,
        }


@dataclass(frozen=True)
class CognitiveCoreResult:
    decision: CognitiveDecisionV3
    state: ConversationState


class SemanticInterpreter(Protocol):
    async def interpret(self, turn: CognitiveTurn, state: ConversationState) -> SemanticInterpretation: ...


class SemanticAdmissionResult(Protocol):
    interpretation: SemanticInterpretation
    tickets: tuple[AdmissionTicket, ...]
    information_pendings: tuple[InformationPending, ...]
    selection_requests: tuple[TrustedSelectionRequest, ...]
    review_items: tuple[SemanticReviewItem, ...]
    deferred_events: tuple[DeferredSemanticEvent, ...]
    trace: AdmissionTrace


class SemanticAdmissionEngine(Protocol):
    def admit(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
        proposal: SemanticInterpretation,
    ) -> SemanticAdmissionResult: ...


class CognitiveCoreV3:
    """Pure cognitive module: semantic interpretation in, decision and next state out."""

    def __init__(
        self,
        interpreter: SemanticInterpreter,
        *,
        recent_context_limit: int = 12,
        admission_engine: SemanticAdmissionEngine | None = None,
        admission_enforced: bool = True,
    ):
        self._interpreter = interpreter
        self._recent_context_limit = max(1, int(recent_context_limit))
        self._admission_engine = admission_engine
        self._admission_enforced = bool(admission_enforced)

    async def process(self, turn: CognitiveTurn, state: ConversationState) -> CognitiveCoreResult:
        _validate_identity(turn, state)
        interpretation = await self._interpreter.interpret(turn, state)
        interpretation = _resolve_context_references(interpretation, state)
        interpretation = _validate_pending_continuations(interpretation, state, turn.occurred_at)
        admission_tickets: tuple[AdmissionTicket, ...] = ()
        admission_information_pendings: tuple[InformationPending, ...] = ()
        admission_selection_requests: tuple[TrustedSelectionRequest, ...] = ()
        admission_review_items: tuple[SemanticReviewItem, ...] = ()
        admission_deferred_events: tuple[DeferredSemanticEvent, ...] = ()
        admission_trace: AdmissionTrace | None = None
        admission_mode: AdmissionMode = "disabled"
        if self._admission_engine is not None:
            admission = self._admission_engine.admit(turn, state, interpretation)
            admission_trace = admission.trace
            # Shadow keeps the engine-native artifacts for authoritative audit.
            # The planner still ignores them unless Admission is enforced, and
            # the persistence adapter stores Shadow artifacts as non-executable.
            admission_tickets = admission.tickets
            admission_information_pendings = admission.information_pendings
            admission_selection_requests = tuple(
                getattr(admission, "selection_requests", ()) or ()
            )
            admission_review_items = tuple(
                getattr(admission, "review_items", ()) or ()
            )
            admission_deferred_events = tuple(
                getattr(admission, "deferred_events", ()) or ()
            )
            if self._admission_enforced:
                interpretation = admission.interpretation
                admission_mode = "enforced"
            else:
                admission_mode = "shadow"
        decision_id = str(
            uuid5(
                NAMESPACE_URL,
                f"agent2-cognitive-v3:{turn.user_id}:{turn.conversation_id}:{turn.message_id}",
            )
        )
        decision = CognitiveDecisionV3(
            decision_id=decision_id,
            intents=interpretation.intents,
            segments=interpretation.segments,
            entities=interpretation.entities,
            confidence=interpretation.confidence,
            required_actions=interpretation.required_actions,
            clarification_need=interpretation.clarification_need,
            context_update=interpretation.context_update,
            source_text_hash=hashlib.sha256(turn.text.encode("utf-8")).hexdigest(),
            admission_tickets=admission_tickets,
            admission_information_pendings=admission_information_pendings,
            admission_selection_requests=admission_selection_requests,
            admission_review_items=admission_review_items,
            admission_deferred_events=admission_deferred_events,
            admission_trace=admission_trace,
            admission_mode=admission_mode,
        )
        return CognitiveCoreResult(decision=decision, state=self._apply_state(turn, state, interpretation, decision_id))

    def _apply_state(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
        interpretation: SemanticInterpretation,
        context_id: str,
    ) -> ConversationState:
        update = interpretation.context_update
        current_goal = state.current_goal
        goal_stack = state.goal_stack
        if update.clear_current_goal:
            cleared_intent = current_goal.intent if current_goal is not None else ""
            current_goal = None
            if cleared_intent:
                goal_stack = tuple(
                    goal for goal in goal_stack if goal.intent != cleared_intent
                )
        if update.resume_previous_goal and not update.current_goal and goal_stack:
            current_goal = goal_stack[-1]
            goal_stack = goal_stack[:-1]
        if not update.preserve_current_goal and update.current_goal:
            if current_goal is not None and current_goal.intent != update.current_goal:
                goal_stack = tuple(
                    goal
                    for goal in (*goal_stack, current_goal)
                    if goal.intent != update.current_goal
                )[-8:]
            current_goal = ConversationGoal(
                intent=update.current_goal,
                entity_ids=update.remember_entity_ids,
                source_context_id=context_id,
            )
        remembered_entity_ids = set(update.remember_entity_ids)
        if update.bind_pending is not None:
            remembered_entity_ids.update(update.bind_pending.entity_ids)
        remembered = tuple(
            entity for entity in interpretation.entities if entity.entity_id in remembered_entity_ids
        )
        current_entities = _merge_entities(state.current_entities, remembered)
        recent_context = state.recent_context
        if update.remember_turn:
            recent_context = (
                *recent_context,
                RecentContextFrame(
                    context_id=context_id,
                    message_id=turn.message_id,
                    intents=interpretation.intents,
                    entity_ids=tuple(
                        entity.entity_id
                        for entity in interpretation.entities
                        if entity.entity_id in remembered_entity_ids
                    ),
                    summary=turn.text.strip(),
                    occurred_at=turn.occurred_at,
                ),
            )[-self._recent_context_limit :]
        # A validated continuation is not consumed until its typed execution
        # succeeds. This prevents a blocked/version-conflicted executor from
        # losing the user's pending operation.
        active_pending = state.active_pending(turn.occurred_at)
        if update.bind_pending is not None:
            request = update.bind_pending
            active_pending = tuple(item for item in active_pending if item.pending_id != request.pending_id) + (
                BoundPending(
                    pending_id=request.pending_id,
                    user_id=turn.user_id,
                    conversation_id=turn.conversation_id,
                    intent=request.intent,
                    action=request.action,
                    entity_ids=request.entity_ids,
                    context_id=context_id,
                    created_at=turn.occurred_at,
                    expires_at=turn.occurred_at + timedelta(seconds=request.expires_in_seconds),
                ),
            )
        user_constraints = _apply_constraint_update(state.user_constraints, update.user_constraints)
        return replace(
            state,
            version=state.version + 1,
            current_goal=current_goal,
            goal_stack=goal_stack,
            current_entities=current_entities,
            recent_context=recent_context,
            pending=active_pending,
            user_constraints=user_constraints,
        )


def _validate_identity(turn: CognitiveTurn, state: ConversationState) -> None:
    if not turn.user_id or not turn.conversation_id or not turn.message_id or not turn.text.strip():
        raise ValueError("cognitive turn requires user, conversation, message, and text")
    if turn.user_id != state.user_id or turn.conversation_id != state.conversation_id:
        raise ValueError("cognitive turn does not belong to conversation state")


def _context_update_payload(update: SemanticContextUpdate) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "current_goal": update.current_goal,
        "preserve_current_goal": update.preserve_current_goal,
        "remember_entity_ids": list(update.remember_entity_ids),
        "remember_turn": update.remember_turn,
        "resume_previous_goal": update.resume_previous_goal,
        "clear_current_goal": update.clear_current_goal,
    }
    if update.bind_pending is not None:
        payload["bind_pending"] = {
            "pending_id": update.bind_pending.pending_id,
            "intent": update.bind_pending.intent,
            "action": update.bind_pending.action,
            "entity_ids": list(update.bind_pending.entity_ids),
            "expires_in_seconds": update.bind_pending.expires_in_seconds,
        }
    if update.user_constraints is not None:
        payload["user_constraints"] = {
            "no_daily_write": update.user_constraints.no_daily_write,
            "read_only": update.user_constraints.read_only,
            "no_history_mutation": update.user_constraints.no_history_mutation,
            "draft_only": update.user_constraints.draft_only,
            "sources": list(update.user_constraints.sources),
        }
    if update.consumed_pending_ids:
        payload["consumed_pending_ids"] = list(update.consumed_pending_ids)
    return payload


def _entity_from_payload(payload: Any) -> ConversationEntity:
    if not isinstance(payload, dict):
        raise ValueError("entity must be an object")
    return ConversationEntity(
        entity_id=str(payload.get("entity_id") or "").strip(),
        entity_type=str(payload.get("entity_type") or "").strip(),
        value=str(payload.get("value") or "").strip(),
        confidence=float(payload.get("confidence", 0)),
        attributes=dict(payload.get("attributes") or {}),
        source_context_id=str(payload.get("source_context_id") or "").strip(),
    )


def _action_from_payload(payload: Any) -> RequiredAction:
    if not isinstance(payload, dict):
        raise ValueError("required action must be an object")
    action = RequiredAction(
        action_id=str(payload.get("action_id") or "").strip(),
        action_type=str(payload.get("action_type") or "").strip(),
        intent=str(payload.get("intent") or "").strip(),
        entity_ids=tuple(str(value).strip() for value in payload.get("entity_ids", []) if str(value).strip()),
        parameters=dict(payload.get("parameters") or {}),
    )
    if not action.action_id or not action.action_type or not action.intent:
        raise ValueError("required action requires id, type, and intent")
    return action


def _segment_from_payload(payload: Any) -> SemanticSegment:
    if not isinstance(payload, dict):
        raise ValueError("semantic segment must be an object")
    text = str(payload.get("text") or "").strip()
    segment = SemanticSegment(
        segment_id=str(payload.get("segment_id") or "").strip(),
        text=text,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        intents=tuple(
            str(value).strip() for value in payload.get("intents", []) if str(value).strip()
        ),
        entity_ids=tuple(
            str(value).strip() for value in payload.get("entity_ids", []) if str(value).strip()
        ),
        action_ids=tuple(
            str(value).strip() for value in payload.get("action_ids", []) if str(value).strip()
        ),
        start_offset=int(payload.get("start_offset", -1)),
        end_offset=int(payload.get("end_offset", -1)),
    )
    if not segment.segment_id or not segment.text or not segment.intents:
        raise ValueError("semantic segment requires id, text, and at least one intent")
    return segment


def _clarification_from_payload(payload: Any) -> ClarificationNeed:
    if not isinstance(payload, dict):
        raise ValueError("clarification_need must be an object")
    result = ClarificationNeed(
        reason=str(payload.get("reason") or "").strip(),
        missing_fields=tuple(str(value).strip() for value in payload.get("missing_fields", []) if str(value).strip()),
        question=str(payload.get("question") or "").strip(),
    )
    if not result.reason or not result.question:
        raise ValueError("clarification_need requires reason and question")
    return result


def _pending_request_from_payload(payload: Any) -> PendingBindingRequest | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("bind_pending must be an object")
    request = PendingBindingRequest(
        pending_id=str(payload.get("pending_id") or "").strip(),
        intent=str(payload.get("intent") or "").strip(),
        action=str(payload.get("action") or "").strip(),
        entity_ids=tuple(str(value).strip() for value in payload.get("entity_ids", []) if str(value).strip()),
        expires_in_seconds=int(payload.get("expires_in_seconds", 0)),
    )
    if not request.pending_id or not request.intent or not request.action or not request.entity_ids:
        raise ValueError("pending requires id, intent, action, and entity bindings")
    if not 1 <= request.expires_in_seconds <= 86400:
        raise ValueError("pending expiry must be between 1 and 86400 seconds")
    return request


def _constraint_update_from_payload(payload: Any) -> UserConstraintUpdate | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("user_constraints must be an object")
    return UserConstraintUpdate(
        no_daily_write=_optional_bool(payload, "no_daily_write"),
        read_only=_optional_bool(payload, "read_only"),
        no_history_mutation=_optional_bool(payload, "no_history_mutation"),
        draft_only=_optional_bool(payload, "draft_only"),
        sources=tuple(str(value).strip() for value in payload.get("sources", []) if str(value).strip()),
    )


def _optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} constraint must be boolean")
    return value


def _apply_constraint_update(
    current: UserConstraints,
    update: UserConstraintUpdate | None,
) -> UserConstraints:
    if update is None:
        return current
    return UserConstraints(
        no_daily_write=current.no_daily_write if update.no_daily_write is None else update.no_daily_write,
        read_only=current.read_only if update.read_only is None else update.read_only,
        no_history_mutation=(
            current.no_history_mutation
            if update.no_history_mutation is None
            else update.no_history_mutation
        ),
        draft_only=current.draft_only if update.draft_only is None else update.draft_only,
        sources=tuple(dict.fromkeys((*current.sources, *update.sources))),
    )


def _merge_entities(
    existing: tuple[ConversationEntity, ...],
    incoming: tuple[ConversationEntity, ...],
) -> tuple[ConversationEntity, ...]:
    merged = {entity.entity_id: entity for entity in existing}
    for entity in incoming:
        merged[entity.entity_id] = entity
    return tuple(merged.values())


def _resolve_context_references(
    interpretation: SemanticInterpretation,
    state: ConversationState,
) -> SemanticInterpretation:
    resolved_entities: list[ConversationEntity] = []
    unresolved_ids: set[str] = set()
    for entity in interpretation.entities:
        reference = entity.attributes.get("context_reference")
        if not isinstance(reference, dict):
            resolved_entities.append(entity)
            continue
        intent = str(reference.get("intent") or "").strip()
        referenced_context_id = str(reference.get("context_id") or "").strip()
        selection = str(reference.get("selection") or "").strip()
        value_source = str(reference.get("value_source") or "").strip()
        if referenced_context_id:
            match = next(
                (frame for frame in state.recent_context if frame.context_id == referenced_context_id),
                None,
            )
            reference_is_valid = value_source == "summary" and match is not None
        else:
            match = next(
                (
                    frame
                    for frame in reversed(state.recent_context)
                    if intent and intent in frame.intents
                ),
                None,
            )
            reference_is_valid = selection == "latest" and value_source == "summary" and match is not None
        if not reference_is_valid or match is None:
            unresolved_ids.add(entity.entity_id)
            resolved_entities.append(entity)
            continue
        resolved_entities.append(
            replace(
                entity,
                value=match.summary,
                source_context_id=match.context_id,
            )
        )
    if not unresolved_ids:
        return replace(interpretation, entities=tuple(resolved_entities))
    safe_actions = tuple(
        action for action in interpretation.required_actions if not unresolved_ids.intersection(action.entity_ids)
    )
    clarification = interpretation.clarification_need or ClarificationNeed(
        reason="context_reference_unresolved",
        missing_fields=("context_reference",),
        question="你想补充刚才讨论的哪一项内容？",
    )
    return replace(
        interpretation,
        entities=tuple(resolved_entities),
        required_actions=safe_actions,
        clarification_need=clarification,
    )


def _validate_pending_continuations(
    interpretation: SemanticInterpretation,
    state: ConversationState,
    now: datetime,
) -> SemanticInterpretation:
    active = state.active_pending(now)
    safe_actions: list[RequiredAction] = []
    consumed_pending_ids: list[str] = []
    mismatch = False
    for action in interpretation.required_actions:
        if action.action_type == "clear_daily_report":
            # Whole-report clear is never directly model-authorized. Only the
            # validated continue_pending branch below may synthesize it.
            mismatch = True
            continue
        if action.action_type != "continue_pending":
            safe_actions.append(action)
            continue
        pending_id = str(action.parameters.get("pending_id") or "").strip()
        bound_action = str(action.parameters.get("bound_action") or "").strip()
        matches = [item for item in active if item.pending_id == pending_id]
        if (
            len(active) != 1
            or len(matches) != 1
            or matches[0].intent != action.intent
            or matches[0].action != bound_action
            or matches[0].entity_ids != action.entity_ids
        ):
            mismatch = True
            continue
        matched = matches[0]
        safe_actions.append(
            replace(
                action,
                action_type=matched.action,
                intent=matched.intent,
                entity_ids=matched.entity_ids,
                parameters={"confirmed_pending_id": matched.pending_id},
            )
        )
        consumed_pending_ids.append(matched.pending_id)
    if not mismatch:
        return replace(
            interpretation,
            required_actions=tuple(safe_actions),
            context_update=replace(
                interpretation.context_update,
                consumed_pending_ids=tuple(consumed_pending_ids),
            ),
        )
    return replace(
        interpretation,
        required_actions=tuple(safe_actions),
        clarification_need=interpretation.clarification_need
        or ClarificationNeed(
            reason="pending_binding_mismatch",
            missing_fields=("pending_binding",),
            question="这条确认没有匹配到唯一的待处理事项，请说明要继续哪一项。",
        ),
    )
