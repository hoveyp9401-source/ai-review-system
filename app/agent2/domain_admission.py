from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    ADMISSION_POLICY_VERSION,
    AdmissionMode,
    AdmissionDecision,
    DeferredSemanticEvent,
    InformationPending,
    AdmissionTicket,
    AdmissionTrace,
    SemanticReviewItem,
    TrustedSelectionCandidateRef,
    TrustedSelectionRequest,
)
from app.agent2.cognitive_core_v3 import (
    CognitiveTurn,
    PendingBindingRequest,
    RequiredAction,
    SemanticInterpretation,
    SemanticSegment,
)
from app.agent2.conversation_state import ConversationEntity, ConversationState
from app.agent2.case_statement_contract import assess_case_progress_statement
from app.agent2.business.case_reference import (
    discover_visible_case_references,
    match_grounded_visible_cases,
)
from app.agent2.report_document_contract import (
    daily_item_has_termination_state,
    daily_item_is_control_command,
    daily_item_is_nominal_termination_work,
    daily_item_is_unpunctuated_question,
    daily_semantic_detection_copy,
    parse_structured_daily_document,
    parse_structured_daily_section,
)
from app.agent2.selection_pending import protect_selection_continuation_payload


_DAILY_REPORT_FIELDS = ("today_work", "problems", "tomorrow_plan")
_DAILY_DRAFT_MUTABLE_STATUSES = frozenset({"collecting", "pending_confirmation"})
_DAILY_SECTION_ASSERTION_PREFIX = re.compile(
    r"^\s*(?:"
    r"【(?:今日|今天|当日)(?:工作|完成)】"
    r"|【(?:问题与风险|风险与问题|问题和风险|风险和问题|问题/风险|风险/问题|问题|风险)】"
    r"|【(?:明日|明天|次日)计划】"
    r"|\[(?:今日|今天|当日)(?:工作|完成)\]"
    r"|\[(?:问题与风险|风险与问题|问题和风险|风险和问题|问题/风险|风险/问题|问题|风险)\]"
    r"|\[(?:明日|明天|次日)计划\]"
    r"|(?:今日|今天|当日)(?:工作|完成)"
    r"|(?:问题与风险|风险与问题|问题和风险|风险和问题|问题/风险|风险/问题|问题|风险)"
    r"|(?:明日|明天|次日)计划"
    r")(?:(?:就是|是|为)|[：:])?\s*"
)
_DAILY_ITEM_ASSERTION_PREFIX = re.compile(
    r"^\s*(?:(?:\d+|[一二三四五六七八九十百]+)[.、．)）]"
    r"|\((?:\d+|[一二三四五六七八九十百]+)\)"
    r"|（(?:\d+|[一二三四五六七八九十百]+)）"
    r"|[-*•·])\s*"
)
_DAILY_DROPPED_POLARITY_PREFIX = re.compile(
    r"(?:不是|并非|没有|尚未|还没|并未|未曾|未能|没能|不能|不得|"
    r"不再|无需|无须|取消|撤销|撤回|停止|暂停|放弃|算了|不|没|未|别|勿)$"
)
_DAILY_DROPPED_POLARITY_SUFFIX = re.compile(
    r"^(?:算了|不去|不做|不处理|不跟进|不再|不了|不成|不上)"
)
_DAILY_NONAFFIRMATIVE_LEADING = re.compile(
    r"^(?:不|没|未|尚未|还没|并未|别|勿|无需|无须|取消|撤销|撤回|"
    r"停止|暂停|放弃|算了)"
)
_PERIODIC_REPORT_FIELDS = ("accomplishments", "risks", "next_plan", "metrics")
_READ_ONLY_REPORT_ACTIONS = frozenset(
    {"query_daily_report", "query_periodic_report"}
)
_READ_ONLY_ACTIONS = _READ_ONLY_REPORT_ACTIONS | frozenset(
    {
        "answer_case_query",
        "query_case_progress",
        "query_operation_status",
        "search_enterprise_knowledge",
    }
)
_REPORT_ACTIONS = frozenset(
    {
        "capture_daily_event",
        "submit_daily_report",
        "edit_daily_item",
        "delete_daily_item",
        "merge_daily_items",
        "replace_daily_section",
        "move_daily_items",
        "query_daily_report",
        "clear_daily_section",
        "clear_daily_report",
        "reopen_daily_report",
        "copy_previous_daily_report",
        "copy_current_work_to_tomorrow",
        "complete_previous_daily_plan",
        "capture_report_event",
        "query_periodic_report",
        "submit_periodic_report",
        "edit_periodic_report_item",
        "delete_periodic_report_item",
    }
)
_REPORT_MUTATION_CONTRACTS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "capture_daily_event": ("append_item", ("section", "items")),
    "submit_daily_report": ("submit_report", ("status",)),
    "edit_daily_item": ("edit_item", ("items",)),
    "delete_daily_item": ("delete_item", ("items",)),
    "merge_daily_items": ("merge_items", ("items",)),
    "replace_daily_section": ("replace_section", ("section", "items")),
    "move_daily_items": ("move_items", ("section", "items")),
    "clear_daily_section": ("clear_report", ("section", "items")),
    "clear_daily_report": ("clear_report", ("sections", "items")),
    "reopen_daily_report": ("reopen_report", ("status",)),
    "copy_previous_daily_report": ("copy_report", ("sections", "items")),
    "copy_current_work_to_tomorrow": ("copy_report", ("section", "items")),
    "complete_previous_daily_plan": ("copy_report", ("section", "items")),
    "capture_report_event": ("append_item", ("section", "items")),
    "submit_periodic_report": ("submit_report", ("status",)),
    "edit_periodic_report_item": ("edit_item", ("items",)),
    "delete_periodic_report_item": ("delete_item", ("items",)),
}


@dataclass(frozen=True)
class _TrustedDailyReport:
    report_id: str
    report_date: str
    version: int
    status: str
    sections: Mapping[str, tuple[str, ...]]
    item_ids: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class _TrustedPeriodicReport:
    report_id: str
    owner_user_id: str
    report_type: str
    period_key: str
    version: int
    status: str
    sections: Mapping[str, tuple[str, ...]]
    item_ids: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class AssertionPolarityAssessment:
    """Fail-closed validation of one model-proposed mutation assertion."""

    classification: str
    authorizes_mutation: bool
    reason_code: str = ""


@dataclass(frozen=True)
class _ValidatedPendingBinding:
    request: PendingBindingRequest
    segment_id: str
    entity_ids: tuple[str, ...]
    intents: tuple[str, ...]


@dataclass(frozen=True)
class AdmissionCapturePolicy:
    """Trusted switches for audit-only Admission capture; all default off."""

    mode: AdmissionMode = "disabled"
    review_enabled: bool = False
    deferred_enabled: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"disabled", "shadow", "enforced"}:
            raise ValueError("unknown Admission capture mode")
        if not isinstance(self.review_enabled, bool) or not isinstance(
            self.deferred_enabled, bool
        ):
            raise ValueError("Admission capture switches must be boolean")


@dataclass(frozen=True)
class DomainAdmissionResult:
    interpretation: SemanticInterpretation
    decisions: tuple[AdmissionDecision, ...]
    tickets: tuple[AdmissionTicket, ...]
    information_pendings: tuple[InformationPending, ...]
    trace: AdmissionTrace
    selection_requests: tuple[TrustedSelectionRequest, ...] = ()
    review_items: tuple[SemanticReviewItem, ...] = ()
    deferred_events: tuple[DeferredSemanticEvent, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": ADMISSION_CONTRACT_VERSION,
            "trace": self.trace.as_dict(),
            "decisions": [decision.as_dict() for decision in self.decisions],
            "tickets": [ticket.as_dict() for ticket in self.tickets],
            "review_items": [item.as_dict() for item in self.review_items],
            "deferred_events": [event.as_dict() for event in self.deferred_events],
            "information_pendings": [
                pending.as_dict() for pending in self.information_pendings
            ],
            "selection_requests": [
                request.as_dict() for request in self.selection_requests
            ],
        }


class DomainAdmissionEngine:
    """Turn-scoped semantic write admission.

    The public interface accepts one model proposal plus trusted turn/state data
    and returns the only interpretation that downstream planners may consume.
    """

    _TICKET_TTL = timedelta(minutes=5)
    _INFORMATION_PENDING_TTL = timedelta(minutes=10)
    _SELECTION_PENDING_TTL = timedelta(minutes=10)

    def __init__(
        self,
        *,
        capture_policy: AdmissionCapturePolicy | None = None,
    ) -> None:
        self._capture_policy = capture_policy or AdmissionCapturePolicy()

    def admit(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
        proposal: SemanticInterpretation,
    ) -> DomainAdmissionResult:
        scope = _trusted_scope(turn, state)
        created_at = _aware(turn.occurred_at)
        proposal_sha256 = _semantic_proposal_sha256(proposal)
        source_turn_id = turn.message_id
        trace_id = _stable_uuid(
            "trace",
            scope["tenant_id"],
            scope["user_id"],
            turn.conversation_id,
            source_turn_id,
            turn.message_id,
            str(state.version),
            proposal_sha256,
            ADMISSION_POLICY_VERSION,
        )
        entity_by_id = {entity.entity_id: entity for entity in proposal.entities}
        segments_by_action: dict[str, list[SemanticSegment]] = {}
        for segment in proposal.segments:
            for action_id in segment.action_ids:
                segments_by_action.setdefault(action_id, []).append(segment)
        actions_to_evaluate = list(proposal.required_actions)
        decisions: list[AdmissionDecision] = []
        tickets: list[AdmissionTicket] = []
        information_pendings: list[InformationPending] = []
        selection_requests: list[TrustedSelectionRequest] = []
        admitted_actions: list[RequiredAction] = []
        grounded_segments: dict[str, SemanticSegment] = {}
        report_version_offsets: dict[tuple[str, str], int] = {}

        for action in actions_to_evaluate:
            bound_segments = tuple(segments_by_action.get(action.action_id, ()))
            segment = bound_segments[0] if len(bound_segments) == 1 else None
            grounded_segment, binding_reason = _ground_action_segment(
                turn=turn,
                action=action,
                segment=segment,
                bound_segment_count=len(bound_segments),
            )
            segment_id = segment.segment_id if segment is not None else "unbound"
            if binding_reason:
                domain, operation = _action_contract(action)
                decision = AdmissionDecision(
                    action_id=action.action_id,
                    segment_id=segment_id,
                    domain=domain,
                    operation=operation,
                    status="blocked",
                    reason_code=binding_reason,
                )
                object_ref = {}
            else:
                assert grounded_segment is not None
                segment = grounded_segment
                grounded_segments[segment.segment_id] = segment
                decision, object_ref = self._decide(
                    turn=turn,
                    state=state,
                    action=action,
                    segment_id=segment_id,
                    segment_text=segment.text if segment is not None else "",
                    segment_entity_ids=(
                        segment.entity_ids if segment is not None else ()
                    ),
                    entity_by_id=entity_by_id,
                )
                if decision.status == "admitted" and decision.domain == "report":
                    object_ref = _rebind_report_object_version(
                        object_ref,
                        version_offsets=report_version_offsets,
                    )
            segment_for_evidence = grounded_segment or segment
            segment_hash = (
                segment_for_evidence.text_hash
                if segment_for_evidence is not None
                else hashlib.sha256(b"").hexdigest()
            )
            segment_start = (
                segment_for_evidence.start_offset
                if segment_for_evidence is not None
                and segment_for_evidence.start_offset >= 0
                else 0
            )
            segment_end = (
                segment_for_evidence.end_offset
                if segment_for_evidence is not None
                and segment_for_evidence.end_offset >= segment_start
                else segment_start
            )
            persisted_object_ref = (
                _stable_object_ref(
                    trace_id=trace_id,
                    action=action,
                    domain=decision.domain,
                    raw_object_ref=object_ref,
                )
                if decision.status in {"admitted", "information_required"}
                and object_ref
                else None
            )
            decision_id = _stable_uuid(
                "decision",
                trace_id,
                action.action_id,
                segment_id,
                segment_hash,
                decision.domain,
                decision.operation,
                decision.status,
                _canonical_json(persisted_object_ref),
            )
            decision = replace(
                decision,
                decision_id=decision_id,
                trace_id=trace_id,
                tenant_id=scope["tenant_id"],
                user_id=scope["user_id"],
                conversation_id=turn.conversation_id,
                source_turn_id=source_turn_id,
                source_message_id=turn.message_id,
                segment_text_sha256=segment_hash,
                segment_start_offset=segment_start,
                segment_end_offset=segment_end,
                object_ref=persisted_object_ref,
                expected_conversation_state_version=state.version,
                evidence_refs=(f"segment_sha256:{segment_hash}",),
                idempotency_key=_stable_digest(
                    "decision",
                    scope["tenant_id"],
                    decision_id,
                ),
                created_at=created_at,
            )
            selection_payload = object_ref.get("trusted_selection") if isinstance(
                object_ref, Mapping
            ) else None
            if (
                decision.status == "blocked"
                and decision.reason_code == "case_reference_ambiguous"
                and isinstance(selection_payload, Mapping)
                and segment_for_evidence is not None
            ):
                selection_request = _create_trusted_selection_request(
                    turn=turn,
                    scope=scope,
                    trace_id=trace_id,
                    decision_id=decision_id,
                    source_turn_id=source_turn_id,
                    expected_conversation_state_version=state.version + 1,
                    action=action,
                    entity_by_id=entity_by_id,
                    segment=segment_for_evidence,
                    candidate_records=selection_payload.get("candidates"),
                    created_at=created_at,
                    ttl=self._SELECTION_PENDING_TTL,
                )
                if selection_request is not None:
                    # pending_id stays empty: the SQL FK is reserved for
                    # InformationPending.  Association is audit-only here.
                    decision = replace(
                        decision,
                        evidence_refs=(
                            *decision.evidence_refs,
                            f"selection_request:{selection_request.selection_request_id}",
                        ),
                    )
                    selection_requests.append(selection_request)
            if decision.status == "admitted":
                admitted_actions.append(action)
                if _action_requires_execution_ticket(action.action_type):
                    assert segment_for_evidence is not None
                    authority_scope, allowed_changed_fields, fact_claims_sha256 = (
                        _authorization_claims(
                            action=action,
                            operation=decision.operation,
                            entity_by_id=entity_by_id,
                            segment=segment_for_evidence,
                            raw_object_ref=object_ref,
                        )
                    )
                    ticket = _issue_ticket(
                        turn=turn,
                        scope=scope,
                        trace_id=trace_id,
                        decision_id=decision_id,
                        source_turn_id=source_turn_id,
                        expected_conversation_state_version=state.version,
                        action_id=action.action_id,
                        segment_id=segment_id,
                        segment_text_sha256=segment_hash,
                        segment_start_offset=segment_start,
                        segment_end_offset=segment_end,
                        domain=decision.domain,
                        operation=decision.operation,
                        object_ref=persisted_object_ref or {},
                        authority_scope=authority_scope,
                        allowed_changed_fields=allowed_changed_fields,
                        fact_claims_sha256=fact_claims_sha256,
                        ttl=self._TICKET_TTL,
                    )
                    decision = replace(decision, ticket_id=ticket.ticket_id)
                    tickets.append(ticket)
                    if decision.domain == "report":
                        report_key = _report_object_key(object_ref)
                        if report_key is not None:
                            report_version_offsets[report_key] = (
                                report_version_offsets.get(report_key, 0) + 1
                            )
            elif decision.status == "information_required":
                pending = _create_information_pending(
                    turn=turn,
                    scope=scope,
                    trace_id=trace_id,
                    decision_id=decision_id,
                    source_turn_id=source_turn_id,
                    expected_conversation_state_version=state.version,
                    action=action,
                    entity_by_id=entity_by_id,
                    segment_id=segment_id,
                    segment_text_sha256=segment_hash,
                    segment_start_offset=segment_start,
                    segment_end_offset=segment_end,
                    domain=decision.domain,
                    operation=decision.operation,
                    object_ref=persisted_object_ref,
                    created_at=created_at,
                    ttl=self._INFORMATION_PENDING_TTL,
                )
                decision = replace(decision, pending_id=pending.pending_id)
                information_pendings.append(pending)
            decisions.append(decision)

        validated_pending = _validate_pending_binding(
            turn=turn,
            state=state,
            proposal=proposal,
            trace_id=trace_id,
            entity_by_id=entity_by_id,
        )
        pending_entity_ids = set(
            validated_pending.entity_ids if validated_pending is not None else ()
        )
        pending_intents = set(
            validated_pending.intents if validated_pending is not None else ()
        )
        pending_segment_ids = {
            validated_pending.segment_id
        } if validated_pending is not None else set()

        admitted_ids = {action.action_id for action in admitted_actions}
        admitted_entity_ids = {
            entity_id
            for action in admitted_actions
            for entity_id in action.entity_ids
        } | pending_entity_ids
        admitted_intents = {action.intent for action in admitted_actions} | pending_intents
        no_op_intents = {
            action.intent
            for action, decision in zip(actions_to_evaluate, decisions, strict=True)
            if decision.status == "no_op"
        }
        update = proposal.context_update
        if actions_to_evaluate:
            authorized_goal_intents = admitted_intents | no_op_intents
            goal_authorized = not update.current_goal or update.current_goal in authorized_goal_intents
            pending_authorized = update.bind_pending is None or validated_pending is not None
            update = replace(
                update,
                current_goal=update.current_goal if goal_authorized else "",
                preserve_current_goal=update.preserve_current_goal or not goal_authorized,
                remember_entity_ids=tuple(
                    entity_id
                    for entity_id in update.remember_entity_ids
                    if entity_id in admitted_entity_ids
                ),
                bind_pending=(
                    validated_pending.request
                    if validated_pending is not None
                    else None
                ) if pending_authorized else None,
                remember_turn=update.remember_turn and bool(
                    admitted_actions or validated_pending is not None
                ),
            )
        elif update.bind_pending is not None:
            update = replace(
                update,
                bind_pending=(
                    validated_pending.request
                    if validated_pending is not None
                    else None
                ),
                remember_turn=update.remember_turn and validated_pending is not None,
            )
        ambiguous_case_clarification = (
            proposal.clarification_need is not None
            and proposal.clarification_need.reason == "ambiguous_case_alias"
        )
        pending_mismatch_clarification = (
            proposal.clarification_need is not None
            and proposal.clarification_need.reason == "pending_binding_mismatch"
            and not admitted_actions
        )
        if ambiguous_case_clarification and not admitted_actions:
            active_pending_ids = tuple(
                item.pending_id for item in state.active_pending(created_at)
            )
            preserves_lifecycle_revocation = (
                update.pending_invalidation_reason
                == "superseded_by_new_instruction"
                and tuple(update.invalidated_pending_ids) == active_pending_ids
                and bool(active_pending_ids)
            )
            update = replace(
                update,
                current_goal="",
                preserve_current_goal=True,
                remember_entity_ids=(),
                remember_turn=False,
                bind_pending=None,
                user_constraints=None,
                consumed_pending_ids=(),
                invalidated_pending_ids=(
                    active_pending_ids if preserves_lifecycle_revocation else ()
                ),
                pending_invalidation_reason=(
                    "superseded_by_new_instruction"
                    if preserves_lifecycle_revocation
                    else ""
                ),
                resume_previous_goal=False,
                clear_current_goal=False,
            )
        retained_segments: list[SemanticSegment] = []
        for segment in proposal.segments:
            action_ids = tuple(
                action_id for action_id in segment.action_ids if action_id in admitted_ids
            )
            if not action_ids and segment.segment_id not in pending_segment_ids:
                continue
            segment_entity_ids = tuple(
                entity_id
                for entity_id in segment.entity_ids
                if entity_id in admitted_entity_ids
            )
            segment_intents = tuple(
                intent for intent in segment.intents if intent in admitted_intents
            )
            retained_segments.append(
                replace(
                    grounded_segments.get(segment.segment_id, segment),
                    action_ids=action_ids,
                    entity_ids=segment_entity_ids,
                    intents=segment_intents,
                )
            )
        interpretation = replace(
            proposal,
            intents=tuple(
                intent for intent in proposal.intents if intent in admitted_intents
            ),
            entities=tuple(
                entity
                for entity in proposal.entities
                if entity.entity_id in admitted_entity_ids
            ),
            required_actions=tuple(admitted_actions),
            context_update=update,
            segments=tuple(retained_segments),
            clarification_need=(
                proposal.clarification_need
                if (
                    ambiguous_case_clarification
                    or pending_mismatch_clarification
                    or validated_pending is not None
                )
                else None
            ),
        )
        decision_tuple = tuple(decisions)
        review_items, deferred_events = _capture_decision_audit_artifacts(
            decisions=decision_tuple,
            policy=self._capture_policy,
            created_at=created_at,
        )
        trace = AdmissionTrace(
            trace_id=trace_id,
            tenant_id=scope["tenant_id"],
            user_id=scope["user_id"],
            conversation_id=turn.conversation_id,
            source_message_id=turn.message_id,
            contract_version=ADMISSION_CONTRACT_VERSION,
            decisions=decision_tuple,
            source_turn_id=source_turn_id,
            expected_conversation_state_version=state.version,
            proposal_sha256=proposal_sha256,
            policy_version=ADMISSION_POLICY_VERSION,
            admission_summary=_admission_summary(decision_tuple),
            idempotency_key=_stable_digest(
                "trace", scope["tenant_id"], trace_id
            ),
            created_at=created_at,
        )
        return DomainAdmissionResult(
            interpretation=interpretation,
            decisions=decision_tuple,
            tickets=tuple(tickets),
            information_pendings=tuple(information_pendings),
            trace=trace,
            selection_requests=tuple(selection_requests),
            review_items=review_items,
            deferred_events=deferred_events,
        )

    def _decide(
        self,
        *,
        turn: CognitiveTurn,
        state: ConversationState,
        action: RequiredAction,
        segment_id: str,
        segment_text: str,
        segment_entity_ids: tuple[str, ...],
        entity_by_id: Mapping[str, ConversationEntity],
    ) -> tuple[AdmissionDecision, Mapping[str, Any]]:
        if action.action_type in _REPORT_ACTIONS:
            return _decide_report_action(
                turn=turn,
                state=state,
                action=action,
                segment_id=segment_id,
                segment_text=segment_text,
                segment_entity_ids=segment_entity_ids,
                entity_by_id=entity_by_id,
            )

        if action.action_type == "record_case_progress":
            case_entity = _single_entity(action, entity_by_id)
            case_matches = _matching_visible_cases_from_segment(
                segment_text,
                turn.resources.get("visible_cases"),
                grounded_reference=(case_entity.value if case_entity is not None else ""),
            )
            case_record = case_matches[0] if len(case_matches) == 1 else None
            if len(case_matches) > 1:
                if case_entity is None or case_entity.entity_type != "case_ref":
                    return (
                        AdmissionDecision(
                            action_id=action.action_id,
                            segment_id=segment_id,
                            domain="case",
                            operation="record_case_progress",
                            status="blocked",
                            reason_code="case_reference_not_grounded_in_segment",
                        ),
                        {},
                    )
                fact_contract_reason = _case_fact_contract_reason(
                    case_entity,
                    segment_text,
                )
                if fact_contract_reason:
                    return (
                        AdmissionDecision(
                            action_id=action.action_id,
                            segment_id=segment_id,
                            domain="case",
                            operation="record_case_progress",
                            status="blocked",
                            reason_code=fact_contract_reason,
                        ),
                        {},
                    )
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="case",
                        operation="record_case_progress",
                        status="blocked",
                        reason_code="case_reference_ambiguous",
                    ),
                    {
                        "trusted_selection": {
                            "candidates": [dict(item) for item in case_matches],
                        }
                    },
                )
            if case_record is not None:
                if case_entity is None or case_entity.entity_type != "case_ref":
                    return (
                        AdmissionDecision(
                            action_id=action.action_id,
                            segment_id=segment_id,
                            domain="case",
                            operation="record_case_progress",
                            status="blocked",
                            reason_code="case_reference_not_grounded_in_segment",
                        ),
                        {},
                    )
                attributes = (
                    case_entity.attributes if case_entity is not None else {}
                )
                requested_snooze = str(
                    attributes.get("requested_snooze") or ""
                ).strip()
                if requested_snooze:
                    followup = _resolve_active_case_followup(
                        turn=turn,
                        case_id=str(case_record["case_id"]),
                        pending_id=str(
                            attributes.get("followup_notification_id") or ""
                        ).strip(),
                    )
                    snoozed_until = _resolve_followup_snoozed_until(
                        requested_snooze,
                        now=_aware(turn.occurred_at),
                    )
                    if followup is None or snoozed_until is None:
                        return (
                            AdmissionDecision(
                                action_id=action.action_id,
                                segment_id=segment_id,
                                domain="case",
                                operation="snooze_case_followup",
                                status="blocked",
                                reason_code="case_followup_snooze_not_uniquely_authorized",
                            ),
                            {},
                        )
                    authority_scope = {
                        "pending_id": str(followup["pending_id"]),
                        "pending_version": int(followup["pending_version"]),
                        "followup_id": str(followup["followup_id"]),
                        "task_id": str(followup["task_id"]),
                        "task_version": int(followup["task_version"]),
                        "case_id": str(followup["case_id"]),
                        "case_version": int(followup["case_version"]),
                        "policy_id": str(followup.get("policy_id") or ""),
                        "policy_version": int(followup.get("policy_version") or 0),
                        "assigned_user_id": str(followup["assigned_user_id"]),
                        "conversation_id": str(followup["conversation_id"]),
                        "expected_state_version": int(
                            followup["expected_state_version"]
                        ),
                        "requested_snooze": requested_snooze,
                        "snoozed_until": snoozed_until.isoformat(),
                        "raw_fact": segment_text,
                    }
                    return (
                        AdmissionDecision(
                            action_id=action.action_id,
                            segment_id=segment_id,
                            domain="case",
                            operation="snooze_case_followup",
                            status="admitted",
                            reason_code="case_followup_snooze_uniquely_authorized",
                        ),
                        {
                            "object_type": "case_followup_pending",
                            "stable_id": str(followup["pending_id"]),
                            "version": int(followup["pending_version"]),
                            "authority_scope": authority_scope,
                            "allowed_changed_fields": [
                                "task_status",
                                "task_response_status",
                                "task_next_eligible_at",
                                "task_completed_at",
                                "task_version",
                                "pending_status",
                                "pending_consumed_at",
                                "pending_version",
                                "policy_snoozed_until",
                                "policy_next_due_at",
                                "policy_version",
                            ],
                        },
                    )
                fact_contract_reason = _case_fact_contract_reason(
                    case_entity,
                    segment_text,
                )
                if fact_contract_reason:
                    return (
                        AdmissionDecision(
                            action_id=action.action_id,
                            segment_id=segment_id,
                            domain="case",
                            operation="record_case_progress",
                            status="blocked",
                            reason_code=fact_contract_reason,
                        ),
                        {},
                    )
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="case",
                        operation="record_case_progress",
                        status="admitted",
                        reason_code="case_reference_uniquely_authorized",
                    ),
                    {
                        "case_id": str(case_record["case_id"]),
                        "version": int(case_record.get("version") or 0),
                    },
                )
            reference_grounded = (
                case_entity is not None
                and _normalize_reference(case_entity.value)
                in _normalize_reference(segment_text)
            )
            return (
                AdmissionDecision(
                    action_id=action.action_id,
                    segment_id=segment_id,
                    domain="case",
                    operation="record_case_progress",
                    status="blocked",
                    reason_code=(
                        "case_reference_not_uniquely_authorized"
                        if reference_grounded
                        else "case_reference_not_grounded_in_segment"
                    ),
                ),
                {},
            )

        if action.action_type == "query_case_progress":
            entity = _single_entity(action, entity_by_id)
            attributes = entity.attributes if entity is not None else {}
            case_hint = str(attributes.get("case_hint") or "").strip()
            case_record = _resolve_visible_case(
                case_hint,
                turn.resources.get("visible_cases"),
            )
            if (
                entity is None
                or not case_hint
                or _normalize_reference(case_hint)
                not in _normalize_reference(segment_text)
                or case_record is None
            ):
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="case",
                        operation="query_case_progress",
                        status="blocked",
                        reason_code="case_query_not_uniquely_authorized",
                    ),
                    {},
                )
            return (
                AdmissionDecision(
                    action_id=action.action_id,
                    segment_id=segment_id,
                    domain="case",
                    operation="query_case_progress",
                    status="admitted",
                    reason_code="case_query_uniquely_authorized",
                ),
                {
                    "case_id": str(case_record["case_id"]),
                    "version": int(case_record.get("version") or 0),
                },
            )

        if action.action_type in {
            "update_case_progress",
            "delete_case_progress",
            "link_case_progress",
        }:
            return _decide_case_progress_mutation(
                turn=turn,
                action=action,
                segment_id=segment_id,
                segment_text=segment_text,
                entity_by_id=entity_by_id,
            )

        if action.action_type == "answer_case_query":
            entity = _single_entity(action, entity_by_id)
            attributes = entity.attributes if entity is not None else {}
            question = str(attributes.get("question") or entity.value if entity is not None else "").strip()
            matter_hint = str(attributes.get("matter_hint") or "").strip()
            raw_visible_cases = turn.resources.get("visible_cases")
            if entity is not None and not _is_direct_case_query(segment_text):
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="case",
                        operation="answer_case_query",
                        status="blocked",
                        reason_code="case_query_direct_question_required",
                    ),
                    {},
                )
            grounded = any(
                _normalize_reference(value)
                and _normalize_reference(value) in _normalize_reference(segment_text)
                for value in (question, matter_hint, entity.value if entity is not None else "")
            )
            if entity is None or not isinstance(raw_visible_cases, list) or not grounded:
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="case",
                        operation="answer_case_query",
                        status="blocked",
                        reason_code="case_query_scope_or_evidence_missing",
                    ),
                    {},
                )
            return (
                AdmissionDecision(
                    action_id=action.action_id,
                    segment_id=segment_id,
                    domain="case",
                    operation="answer_case_query",
                    status="admitted",
                    reason_code="case_query_permission_scope_verified",
                ),
                {
                    "object_type": "case_query",
                    "stable_id": _stable_digest(
                        "case-query", turn.tenant_id, turn.actor_user_id or turn.user_id,
                        question or matter_hint,
                    ),
                    "version": None,
                },
            )
        if action.action_type == "query_operation_status":
            entity = _single_entity(action, entity_by_id)
            attributes = entity.attributes if entity is not None else {}
            domain = str(attributes.get("domain") or "").strip()
            access = turn.resources.get("operation_status_access")
            if (
                entity is None
                or domain not in {"case_progress", "travel"}
                or _normalize_reference(entity.value) != _normalize_reference(segment_text)
                or not _verified_read_access(turn, access)
            ):
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="runtime",
                        operation="query_operation_status",
                        status="blocked",
                        reason_code="operation_status_scope_or_evidence_missing",
                    ),
                    {},
                )
            return (
                AdmissionDecision(
                    action_id=action.action_id,
                    segment_id=segment_id,
                    domain="runtime",
                    operation="query_operation_status",
                    status="admitted",
                    reason_code="operation_status_scope_verified",
                ),
                {
                    "object_type": "operation_status_query",
                    "stable_id": f"{turn.actor_user_id or turn.user_id}:{domain}",
                    "version": None,
                },
            )

        if action.action_type == "search_enterprise_knowledge":
            entity = _single_entity(action, entity_by_id)
            attributes = entity.attributes if entity is not None else {}
            query = str(attributes.get("query") or entity.value if entity is not None else "").strip()
            if (
                entity is None
                or not query
                or _normalize_reference(query) not in _normalize_reference(segment_text)
                or not _verified_read_access(
                    turn,
                    turn.resources.get("enterprise_knowledge_access"),
                )
            ):
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="knowledge",
                        operation="search_enterprise_knowledge",
                        status="blocked",
                        reason_code="knowledge_query_scope_or_evidence_missing",
                    ),
                    {},
                )
            return (
                AdmissionDecision(
                    action_id=action.action_id,
                    segment_id=segment_id,
                    domain="knowledge",
                    operation="search_enterprise_knowledge",
                    status="admitted",
                    reason_code="knowledge_query_scope_verified",
                ),
                {
                    "object_type": "knowledge_query",
                    "stable_id": _stable_digest(
                        "knowledge-query", turn.tenant_id,
                        turn.actor_user_id or turn.user_id, query,
                    ),
                    "version": None,
                },
            )

        if action.action_type == "respond_travel_collaboration":
            entity = _single_entity(action, entity_by_id)
            attributes = entity.attributes if entity is not None else {}
            candidate_id = str(attributes.get("candidate_id") or "").strip()
            response = str(attributes.get("response") or "").strip().casefold()
            raw_candidates = turn.resources.get("active_travel_collaborations")
            active = (
                raw_candidates[0]
                if isinstance(raw_candidates, list)
                and len(raw_candidates) == 1
                and isinstance(raw_candidates[0], Mapping)
                else None
            )
            expires_at = _parse_aware_datetime(
                active.get("expires_at") if active is not None else None
            )
            actor_user_id = str(turn.actor_user_id or turn.user_id or "")
            if (
                entity is None
                or entity.entity_type != "travel_collaboration_ref"
                or active is None
                or not candidate_id
                or str(active.get("candidate_id") or "") != candidate_id
                or actor_user_id not in {
                    str(value) for value in (active.get("participant_ids") or ())
                }
                or str(active.get("status") or "")
                not in {"notified", "accepted_by_one"}
                or expires_at is None
                or expires_at <= _aware(turn.occurred_at)
                or int(active.get("version") or 0) <= 0
                or _canonical_travel_response(segment_text) != response
            ):
                return _blocked_decision(
                    action,
                    segment_id,
                    "travel",
                    "respond_travel_collaboration",
                    "travel_collaboration_context_not_uniquely_authorized",
                )
            version = int(active.get("version") or 0)
            return (
                AdmissionDecision(
                    action_id=action.action_id,
                    segment_id=segment_id,
                    domain="travel",
                    operation="respond_travel_collaboration",
                    status="admitted",
                    reason_code="travel_collaboration_response_authorized",
                ),
                {
                    "object_type": "travel_collaboration_candidate",
                    "stable_id": candidate_id,
                    "version": version,
                    "authority_scope": {
                        "candidate_id": candidate_id,
                        "version": version,
                        "participant_user_id": actor_user_id,
                        "response": response,
                        "raw_fact": segment_text,
                    },
                    "allowed_changed_fields": (
                        "responses_json",
                        "status",
                        "version",
                    ),
                },
            )

        if action.action_type in {
            "update_case_followup_policy",
            "trigger_case_followup_now",
        }:
            return _decide_case_followup_mutation(
                turn=turn,
                action=action,
                segment_id=segment_id,
                segment_text=segment_text,
                entity_by_id=entity_by_id,
            )

        if action.action_type == "update_travel_event":
            return _decide_travel_update(
                turn=turn,
                state=state,
                action=action,
                segment_id=segment_id,
                entity_by_id=entity_by_id,
            )

        if action.action_type == "record_travel_event":
            travel_entity = _single_entity(action, entity_by_id)
            attributes = travel_entity.attributes if travel_entity is not None else {}
            travel_contract_reason = _travel_assertion_contract_reason(
                travel_entity,
                segment_text,
                occurred_at=turn.occurred_at,
                timezone_name=str(turn.resources.get("timezone") or "Asia/Shanghai"),
            )
            if travel_contract_reason:
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="travel",
                        operation="record_travel_event",
                        status="blocked",
                        reason_code=travel_contract_reason,
                    ),
                    {},
                )
            destination = str(attributes.get("destination") or "").strip()
            date_hint = str(attributes.get("date_hint") or "").strip()
            if not destination or _normalize_reference(destination) not in _normalize_reference(
                segment_text
            ):
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="travel",
                        operation="record_travel_event",
                        status="blocked",
                        reason_code="travel_destination_not_grounded_in_segment",
                    ),
                    {},
                )
            travel_date = _resolve_travel_date(
                date_hint,
                occurred_at=turn.occurred_at,
                timezone_name=str(turn.resources.get("timezone") or "Asia/Shanghai"),
            )
            if travel_date is None:
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="travel",
                        operation="record_travel_event",
                        status="information_required",
                        reason_code="travel_time_information_required",
                    ),
                    {
                        "destination": destination,
                        "travel_date": "",
                    },
                )
            if not _travel_date_claim_grounded(
                date_hint,
                segment_text,
                occurred_at=turn.occurred_at,
                timezone_name=str(
                    turn.resources.get("timezone") or "Asia/Shanghai"
                ),
            ):
                return (
                    AdmissionDecision(
                        action_id=action.action_id,
                        segment_id=segment_id,
                        domain="travel",
                        operation="record_travel_event",
                        status="blocked",
                        reason_code="travel_date_not_grounded_in_segment",
                    ),
                    {},
                )
            return (
                AdmissionDecision(
                    action_id=action.action_id,
                    segment_id=segment_id,
                    domain="travel",
                    operation="record_travel_event",
                    status="admitted",
                    reason_code="personal_travel_grounded_and_parseable",
                ),
                {
                    "destination": destination,
                    "travel_date": travel_date,
                },
            )

        return (
            AdmissionDecision(
                action_id=action.action_id,
                segment_id=segment_id,
                domain="runtime",
                operation=action.action_type,
                status="blocked",
                reason_code="unknown_admission_contract",
            ),
            {},
        )


def _validate_pending_binding(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
    proposal: SemanticInterpretation,
    trace_id: str,
    entity_by_id: Mapping[str, ConversationEntity],
) -> _ValidatedPendingBinding | None:
    """Revalidate a model-proposed confirmation without granting it authority.

    The model may describe a pending confirmation, but only trusted resource
    snapshots and the exact source segment can bind the object.  The returned
    pending id is derived by the server so model-controlled ids cannot replace
    an unrelated pending in ConversationState.
    """

    request = proposal.context_update.bind_pending
    clarification = proposal.clarification_need
    if request is None or clarification is None:
        return None
    if len(request.entity_ids) != 1 or not 1 <= request.expires_in_seconds <= 1800:
        return None
    entity = entity_by_id.get(request.entity_ids[0])
    if entity is None:
        return None
    matching_segments = tuple(
        segment
        for segment in proposal.segments
        if set(request.entity_ids).issubset(segment.entity_ids)
        and request.intent in segment.intents
    )
    if len(matching_segments) != 1:
        return None
    segment = matching_segments[0]

    if request.action == "update_travel_event":
        if (
            clarification.reason != "medium_risk_confirmation_required"
            or entity.entity_type != "travel_intent_ref"
        ):
            return None
        attributes = entity.attributes
        travel_intent_id = str(attributes.get("travel_intent_id") or "").strip()
        expected_version = attributes.get("expected_version")
        if (
            not travel_intent_id
            or not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 1
        ):
            return None
        matches = _matching_active_travel_intents(
            turn=turn,
            segment_text=segment.text,
            cancellation=str(attributes.get("new_status") or "") == "cancelled",
        )
        if len(matches) != 1:
            return None
        active = matches[0]
        if (
            str(active.get("travel_intent_id") or "") != travel_intent_id
            or int(active.get("version") or 0) != expected_version
        ):
            return None
        new_status = str(attributes.get("new_status") or "").strip()
        new_date_hint = str(attributes.get("new_date_hint") or "").strip()
        framing = _non_assertive_framing(segment.text)
        if framing:
            return None
        if new_status == "cancelled":
            polarity = evaluate_assertion_polarity_contract(
                domain="travel",
                segment_text=segment.text,
                statement_mode="asserted",
                evidence_fragments=(segment.text,),
                claim_anchors=(
                    str(active.get("destination") or ""),
                    "出差",
                ),
            )
            if polarity.reason_code != "travel_assertion_negated_or_cancelled" or new_date_hint:
                return None
        else:
            if new_status or not new_date_hint:
                return None
            resolved_date = _resolve_travel_date(
                new_date_hint,
                occurred_at=turn.occurred_at,
                timezone_name=str(turn.resources.get("timezone") or "Asia/Shanghai"),
            )
            if resolved_date is None or not _date_hint_is_grounded(new_date_hint, segment.text):
                return None
            current_start = _parse_in_timezone(
                active.get("start_at"),
                str(turn.resources.get("timezone") or "Asia/Shanghai"),
            )
            if current_start is not None and current_start.date().isoformat() == resolved_date:
                return None
    elif request.action == "clear_daily_report":
        if (
            clarification.reason != "high_impact_confirmation_required"
            or entity.entity_type != "daily_report"
        ):
            return None
        current, snapshots = _trusted_daily_report_context(turn.resources)
        if (
            current is None
            or current.status not in _DAILY_DRAFT_MUTABLE_STATUSES
            or _resolve_daily_report(entity, snapshots, current=current) != current
            or not _daily_reference_grounded(entity, current, segment.text)
        ):
            return None
    else:
        return None

    trusted_request = replace(
        request,
        pending_id=_stable_uuid(
            "bound-pending",
            trace_id,
            request.action,
            request.intent,
            entity.entity_id,
        ),
    )
    return _ValidatedPendingBinding(
        request=trusted_request,
        segment_id=segment.segment_id,
        entity_ids=request.entity_ids,
        intents=(request.intent,),
    )


def _confirmed_pending_matches(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
    pending_id: str,
    action: str,
    intent: str,
    entity_ids: tuple[str, ...],
) -> bool:
    matches = tuple(
        pending
        for pending in state.active_pending(turn.occurred_at)
        if pending.pending_id == pending_id
        and pending.user_id == turn.user_id
        and pending.conversation_id == turn.conversation_id
        and pending.action == action
        and pending.intent == intent
        and pending.entity_ids == entity_ids
    )
    return len(matches) == 1 and len(state.active_pending(turn.occurred_at)) == 1


def _decide_travel_update(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
    action: RequiredAction,
    segment_id: str,
    entity_by_id: Mapping[str, ConversationEntity],
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    entity = _single_entity(action, entity_by_id)
    confirmation_id = str(action.parameters.get("confirmed_pending_id") or "").strip()
    if (
        entity is None
        or entity.entity_type != "travel_intent_ref"
        or not confirmation_id
        or not _confirmed_pending_matches(
            turn=turn,
            state=state,
            pending_id=confirmation_id,
            action="update_travel_event",
            intent=action.intent,
            entity_ids=action.entity_ids,
        )
    ):
        return _blocked_decision(
            action,
            segment_id,
            "travel",
            "update_travel_event",
            "verified_confirmation_required",
        )
    attributes = entity.attributes
    travel_intent_id = str(attributes.get("travel_intent_id") or "").strip()
    expected_version = attributes.get("expected_version")
    active_rows = tuple(
        raw
        for raw in turn.resources.get("active_travel_intents") or ()
        if isinstance(raw, Mapping)
        and str(raw.get("travel_intent_id") or "") == travel_intent_id
    )
    if (
        len(active_rows) != 1
        or not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 1
        or int(active_rows[0].get("version") or 0) != expected_version
    ):
        return _blocked_decision(
            action,
            segment_id,
            "travel",
            "update_travel_event",
            "travel_intent_version_or_scope_conflict",
        )
    active = active_rows[0]
    new_status = str(attributes.get("new_status") or "").strip()
    new_date_hint = str(attributes.get("new_date_hint") or "").strip()
    authority_scope: dict[str, Any] = {
        "travel_intent_id": travel_intent_id,
        "version": expected_version,
        "confirmed_pending_id": confirmation_id,
    }
    if new_status == "cancelled" and not new_date_hint:
        authority_scope["status"] = "cancelled"
        allowed_changed_fields = ("status",)
    elif not new_status and new_date_hint:
        resolved_date = _resolve_travel_date(
            new_date_hint,
            occurred_at=turn.occurred_at,
            timezone_name=str(turn.resources.get("timezone") or "Asia/Shanghai"),
        )
        timezone_name = str(turn.resources.get("timezone") or "Asia/Shanghai")
        current_start = _parse_in_timezone(active.get("start_at"), timezone_name)
        current_end = _parse_in_timezone(active.get("end_at"), timezone_name)
        if resolved_date is None or current_start is None or current_end is None:
            return _blocked_decision(
                action,
                segment_id,
                "travel",
                "update_travel_event",
                "travel_update_payload_invalid",
            )
        duration = current_end - current_start
        local_zone = current_start.tzinfo
        target_date = date.fromisoformat(resolved_date)
        new_start = datetime.combine(
            target_date,
            current_start.timetz().replace(tzinfo=None),
            tzinfo=local_zone,
        )
        new_end = new_start + duration
        authority_scope.update(
            {
                "start_at": new_start.isoformat(),
                "end_at": new_end.isoformat(),
                "status": "changed",
            }
        )
        allowed_changed_fields = ("start_at", "end_at", "status")
    else:
        return _blocked_decision(
            action,
            segment_id,
            "travel",
            "update_travel_event",
            "travel_update_payload_invalid",
        )
    return (
        AdmissionDecision(
            action_id=action.action_id,
            segment_id=segment_id,
            domain="travel",
            operation="update_travel_event",
            status="admitted",
            reason_code="confirmed_travel_update_authorized",
        ),
        {
            "object_type": "travel_intent",
            "stable_id": travel_intent_id,
            "version": expected_version,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_changed_fields,
        },
    )


def _matching_active_travel_intents(
    *,
    turn: CognitiveTurn,
    segment_text: str,
    cancellation: bool,
) -> tuple[Mapping[str, Any], ...]:
    rows: list[Mapping[str, Any]] = []
    for raw in turn.resources.get("active_travel_intents") or ():
        if not isinstance(raw, Mapping):
            continue
        travel_intent_id = str(raw.get("travel_intent_id") or "").strip()
        version = raw.get("version")
        status = str(raw.get("status") or "").strip()
        if (
            not travel_intent_id
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
            or status in {"cancelled", "completed"}
        ):
            continue
        rows.append(raw)
    normalized_text = _normalize_reference(segment_text)
    destination_matches = tuple(
        row
        for row in rows
        if (destination := _normalize_reference(row.get("destination")))
        and destination in normalized_text
    )
    candidates = destination_matches or tuple(rows)
    if cancellation:
        date_token = _explicit_relative_date_token(segment_text)
        if date_token:
            expected_date = _resolve_travel_date(
                date_token,
                occurred_at=turn.occurred_at,
                timezone_name=str(turn.resources.get("timezone") or "Asia/Shanghai"),
            )
            dated = tuple(
                row
                for row in candidates
                if (
                    start := _parse_in_timezone(
                        row.get("start_at"),
                        str(turn.resources.get("timezone") or "Asia/Shanghai"),
                    )
                )
                is not None
                and start.date().isoformat() == expected_date
            )
            candidates = dated
    return candidates


def _explicit_relative_date_token(text: str) -> str:
    match = re.search(r"(?:今天|明天|后天|\d{4}-\d{2}-\d{2})", str(text or ""))
    return match.group(0) if match else ""


def _date_hint_is_grounded(date_hint: str, segment_text: str) -> bool:
    normalized_hint = re.sub(r"[_\-]", "", _normalize_reference(date_hint))
    normalized_segment = re.sub(r"[_\-]", "", _normalize_reference(segment_text))
    aliases = {
        "tomorrow": "明天",
        "dayaftertomorrow": "后天",
        "nextmonday": "下周一",
    }
    localized = aliases.get(normalized_hint, normalized_hint)
    return bool(localized and localized in normalized_segment)


def _is_direct_case_query(segment_text: str) -> bool:
    normalized = _normalize_reference(segment_text)
    if not normalized:
        return False
    if "?" in segment_text or "\uff1f" in segment_text:
        return True
    direct_markers = (
        "query",
        "show",
        "find",
        "list",
        "what",
        "which",
        "how",
        "\u67e5\u8be2",
        "\u67e5\u4e00\u4e0b",
        "\u67e5\u4e0b",
        "\u67e5\u770b",
        "\u8bf7\u95ee",
        "\u5e2e\u6211\u67e5",
        "\u6211\u60f3\u77e5\u9053",
        "\u544a\u8bc9\u6211",
        "\u6709\u6ca1\u6709",
        "\u662f\u5426",
        "\u4ec0\u4e48",
        "\u54ea\u4e9b",
        "\u591a\u5c11",
        "\u600e\u4e48",
        "\u5982\u4f55",
        "\u4e3a\u4ec0\u4e48",
        "\u662f\u4e0d\u662f",
        "\u80fd\u5426",
        "\u5230\u54ea",
        "\u600e\u6837",
    )
    return any(marker in normalized for marker in direct_markers)


def _decide_report_action(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
    action: RequiredAction,
    segment_id: str,
    segment_text: str,
    segment_entity_ids: tuple[str, ...],
    entity_by_id: Mapping[str, ConversationEntity],
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    entity = _single_entity(action, entity_by_id)
    if action.action_type in {
        "capture_report_event",
        "query_periodic_report",
        "submit_periodic_report",
        "edit_periodic_report_item",
        "delete_periodic_report_item",
    }:
        return _decide_periodic_report_action(
            turn=turn,
            action=action,
            entity=entity,
            segment_id=segment_id,
            segment_text=segment_text,
        )
    return _decide_daily_report_action(
        turn=turn,
        state=state,
        action=action,
        entity=entity,
        segment_entities=tuple(
            entity_by_id[entity_id]
            for entity_id in segment_entity_ids
            if entity_id in entity_by_id
        ),
        segment_id=segment_id,
        segment_text=segment_text,
    )


def _decide_daily_report_action(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
    action: RequiredAction,
    entity: ConversationEntity | None,
    segment_entities: tuple[ConversationEntity, ...],
    segment_id: str,
    segment_text: str,
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    current, snapshots = _trusted_daily_report_context(turn.resources)
    active_daily = _active_daily_tasks(turn.resources)
    state_daily_goal_active = str(
        getattr(state.current_goal, "intent", "") or ""
    ) in {"daily_report", "daily_append", "daily_modify"}
    if action.action_type == "capture_daily_event":
        capture_contract_reason = _daily_capture_contract_reason(
            entity,
            segment_text,
        )
        if capture_contract_reason:
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code=capture_contract_reason,
            )
    if action.action_type == "capture_daily_event" and len(active_daily) == 1:
        if _matches_report_no_item_answer(entity, active_daily[0]):
            return _report_decision(
                action,
                segment_id,
                status="no_op",
                reason_code="report_no_new_item",
            )

    if action.action_type == "query_daily_report":
        target = _resolve_daily_report(entity, snapshots, current=current)
        if target is None or not _daily_reference_grounded(entity, target, segment_text):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_snapshot_not_uniquely_authorized",
            )
        return _report_decision(
            action,
            segment_id,
            status="admitted",
            reason_code="daily_report_read_authorized",
            object_payload=_daily_object_payload(target),
        )

    capture_target = current
    if action.action_type == "capture_daily_event":
        target_context = _daily_fact_target_context(
            segment_text,
            raw_fact=str(entity.value or "") if entity is not None else "",
        )
        report_refs = tuple(
            candidate
            for candidate in segment_entities
            if candidate.entity_type == "daily_report"
        )
        if len(report_refs) > 1:
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_snapshot_not_uniquely_authorized",
            )
        explicit_target_dates = _explicit_daily_report_target_dates(
            target_context,
            occurred_at=turn.occurred_at,
            timezone_name=str(turn.resources.get("timezone") or "Asia/Shanghai"),
        )
        current_report_date = _current_daily_report_date(turn.resources)
        if (
            not report_refs
            and explicit_target_dates
            and explicit_target_dates != frozenset({current_report_date})
        ):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_snapshot_not_uniquely_authorized",
            )
        if report_refs:
            capture_target = _resolve_daily_report(
                report_refs[0],
                snapshots,
                current=current,
            )
            if capture_target is None or not _daily_mutation_reference_grounded(
                report_refs[0],
                capture_target,
                target_context,
                occurred_at=turn.occurred_at,
                timezone_name=str(turn.resources.get("timezone") or "Asia/Shanghai"),
            ):
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="daily_report_snapshot_not_uniquely_authorized",
                )
            if (
                capture_target.report_date
                != _current_daily_report_date(turn.resources)
                and not _historical_daily_mutation_allowed(turn.resources)
            ):
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="historical_daily_mutation_blocked",
                )

    if current is None:
        return _report_decision(
            action,
            segment_id,
            status="blocked",
            reason_code="daily_report_snapshot_not_uniquely_authorized",
        )
    if current.report_date != _current_daily_report_date(turn.resources):
        return _report_decision(
            action,
            segment_id,
            status="blocked",
            reason_code="historical_daily_mutation_blocked",
        )

    if action.action_type == "capture_daily_event":
        if (
            capture_target is None
            or capture_target.status not in _DAILY_DRAFT_MUTABLE_STATUSES
        ):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="report_context_not_uniquely_authorized",
            )
        explicit_daily_fact = _explicit_standalone_daily_fact_authorized(
            field_name=(
                str(entity.attributes.get("field") or "") if entity else ""
            ),
            segment_text=segment_text,
            raw_fact=(str(entity.value or "") if entity else ""),
        )
        if capture_target == current:
            if (
                len(active_daily) != 1
                and not state_daily_goal_active
                and not explicit_daily_fact
            ):
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="report_context_not_uniquely_authorized",
                )
        elif not _historical_daily_mutation_allowed(turn.resources):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="historical_daily_mutation_blocked",
            )
        field_name = str(entity.attributes.get("field") or "") if entity else ""
        raw_fact = str(entity.value or "").strip() if entity else ""
        if (
            field_name not in _DAILY_REPORT_FIELDS
            or not raw_fact
            or _normalize_reference(raw_fact) not in _normalize_reference(segment_text)
        ):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_fact_not_grounded",
            )
        authority_scope = {
            "report_id": capture_target.report_id,
            "version": capture_target.version,
            "field": field_name,
            "raw_fact": raw_fact,
            "segment_text": segment_text,
        }
        return _report_mutation_admitted(
            action,
            segment_id,
            capture_target,
            authority_scope=authority_scope,
            reason_code=(
                "active_daily_report_authorized"
                if len(active_daily) == 1 or state_daily_goal_active
                else "explicit_daily_fact_authorized"
            ),
        )

    if action.action_type == "submit_daily_report":
        if current.status not in _DAILY_DRAFT_MUTABLE_STATUSES or any(
            not current.sections[field_name] for field_name in _DAILY_REPORT_FIELDS
        ):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_not_writable",
            )
        return _daily_command_admitted(
            action,
            segment_id,
            current,
            command_type="submit_report",
            reason_code="current_daily_report_submit_authorized",
        )

    if action.action_type == "replace_daily_section":
        if current.status not in _DAILY_DRAFT_MUTABLE_STATUSES:
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_not_writable",
            )
        if not _daily_section_replacement_grounded(
            entity,
            current,
            segment_text=segment_text,
            turn_text=turn.text,
        ):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_section_replacement_not_authorized",
            )
        field_name = str(entity.attributes.get("field") or "")
        items = [str(value).strip() for value in entity.attributes.get("items", [])]
        return _daily_command_admitted(
            action,
            segment_id,
            current,
            command_type="replace_section",
            patch={"field": field_name, "items": items},
            reason_code="daily_section_replacement_authorized",
        )

    if action.action_type in {
        "edit_daily_item",
        "delete_daily_item",
        "merge_daily_items",
        "move_daily_items",
    }:
        if current.status not in _DAILY_DRAFT_MUTABLE_STATUSES:
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_not_writable",
            )
        targets = _entity_target_ids(entity)
        expected_minimum = 2 if action.action_type == "merge_daily_items" else 1
        source_field = (
            str(entity.attributes.get("source_field") or "") if entity else ""
        )
        target_field = (
            str(entity.attributes.get("target_field") or "") if entity else ""
        )
        move_target_valid = True
        if action.action_type == "move_daily_items":
            move_target_valid = bool(
                len(targets) == 1
                and source_field in _DAILY_REPORT_FIELDS
                and target_field in _DAILY_REPORT_FIELDS
                and source_field != target_field
                and targets[0] in current.item_ids[source_field]
                and _daily_item_entity_matches_snapshot(
                    entity,
                    current,
                    field_name=source_field,
                    target_id=targets[0],
                )
            )
        if (
            len(targets) < expected_minimum
            or (action.action_type != "merge_daily_items" and len(targets) != 1)
            or len(set(targets)) != len(targets)
            or any(target not in _daily_item_ids(current) for target in targets)
            or not move_target_valid
            or (
                action.action_type == "merge_daily_items"
                and not _daily_targets_share_field(current, targets)
            )
            or not _entity_value_grounded(entity, segment_text)
        ):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_item_target_not_uniquely_authorized",
            )
        patch: dict[str, Any] = {}
        if action.action_type == "move_daily_items":
            patch["target_field"] = target_field
        if action.action_type in {"edit_daily_item", "merge_daily_items"}:
            replacement = str(entity.attributes.get("replacement") or "").strip() if entity else ""
            if action.action_type == "edit_daily_item" and not replacement:
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="daily_item_replacement_not_grounded",
                )
            if replacement:
                if _normalize_reference(replacement) not in _normalize_reference(segment_text):
                    return _report_decision(
                        action,
                        segment_id,
                        status="blocked",
                        reason_code="daily_item_replacement_not_grounded",
                    )
                patch["replacement"] = replacement
        command_type = {
            "edit_daily_item": "edit_item",
            "delete_daily_item": "delete_item",
            "merge_daily_items": "merge_items",
            "move_daily_items": "move_items",
        }[action.action_type]
        return _daily_command_admitted(
            action,
            segment_id,
            current,
            command_type=command_type,
            target_item_ids=targets,
            patch=patch,
            reason_code="daily_item_mutation_authorized",
        )

    if action.action_type in {"clear_daily_section", "clear_daily_report"}:
        target = _resolve_daily_report(entity, snapshots, current=current)
        if target != current or not _daily_reference_grounded(entity, target, segment_text):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_snapshot_not_uniquely_authorized",
            )
        if current.status not in _DAILY_DRAFT_MUTABLE_STATUSES:
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_not_writable",
            )
        if action.action_type == "clear_daily_report":
            confirmation_id = str(action.parameters.get("confirmed_pending_id") or "").strip()
            verified_ids = turn.resources.get("verified_confirmation_ids")
            if (
                not confirmation_id
                or not (
                    _confirmed_pending_matches(
                        turn=turn,
                        state=state,
                        pending_id=confirmation_id,
                        action="clear_daily_report",
                        intent=action.intent,
                        entity_ids=action.entity_ids,
                    )
                    or (
                        isinstance(verified_ids, (list, tuple, set))
                        and confirmation_id in {str(value) for value in verified_ids}
                    )
                )
            ):
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="verified_confirmation_required",
                )
            patch = {"field": "all"}
        else:
            field_name = str(entity.attributes.get("field") or "") if entity else ""
            if field_name not in _DAILY_REPORT_FIELDS:
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="daily_section_not_authorized",
                )
            patch = {"field": field_name}
        return _daily_command_admitted(
            action,
            segment_id,
            current,
            command_type="clear_report",
            patch=patch,
            reason_code="daily_report_clear_authorized",
        )

    if action.action_type == "reopen_daily_report":
        target = _resolve_daily_report(entity, snapshots, current=current)
        if target is None or not _daily_reference_grounded(entity, target, segment_text):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_snapshot_not_uniquely_authorized",
            )
        if target.report_date != _current_daily_report_date(turn.resources):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="historical_daily_mutation_blocked",
            )
        if target != current or target.status != "completed":
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_not_reopenable",
            )
        return _daily_command_admitted(
            action,
            segment_id,
            target,
            command_type="reopen_report",
            patch={"report_date": target.report_date},
            reason_code="current_daily_report_reopen_authorized",
        )

    if action.action_type in {
        "copy_previous_daily_report",
        "copy_current_work_to_tomorrow",
        "complete_previous_daily_plan",
    }:
        if current.status not in _DAILY_DRAFT_MUTABLE_STATUSES:
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_report_not_writable",
            )
        source = _resolve_daily_report(entity, snapshots, current=current)
        if source is None or not _daily_reference_grounded(entity, source, segment_text):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_source_snapshot_not_uniquely_authorized",
            )
        if action.action_type == "copy_previous_daily_report":
            if source.report_id == current.report_id:
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="daily_source_snapshot_not_uniquely_authorized",
                )
            projected_sections = {
                field_name: list(source.sections[field_name])
                for field_name in _DAILY_REPORT_FIELDS
            }
        elif action.action_type == "copy_current_work_to_tomorrow":
            if source.report_id != current.report_id:
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="daily_source_snapshot_not_uniquely_authorized",
                )
            projected_sections = {
                "tomorrow_plan": [
                    _project_current_work_to_tomorrow(value)
                    for value in source.sections["today_work"]
                ]
            }
        else:
            if source.report_id == current.report_id:
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="daily_source_snapshot_not_uniquely_authorized",
                )
            projected_sections = {"today_work": list(source.sections["tomorrow_plan"])}
        if not any(projected_sections.values()):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="daily_source_report_empty",
            )
        return _daily_command_admitted(
            action,
            segment_id,
            current,
            command_type="copy_report",
            patch={
                "sections": projected_sections,
                "source_report_date": source.report_date,
                "source_report_id": source.report_id,
            },
            reason_code="daily_report_projection_authorized",
        )

    return _report_decision(
        action,
        segment_id,
        status="blocked",
        reason_code="unknown_admission_contract",
    )


def _decide_periodic_report_action(
    *,
    turn: CognitiveTurn,
    action: RequiredAction,
    entity: ConversationEntity | None,
    segment_id: str,
    segment_text: str,
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    snapshot = _trusted_periodic_report(
        turn.resources.get("periodic_report"),
        actor_user_id=str(turn.actor_user_id or turn.user_id),
        occurred_at=turn.occurred_at,
        timezone_name=str(turn.resources.get("timezone") or "Asia/Shanghai"),
    )
    if snapshot is None:
        return _report_decision(
            action,
            segment_id,
            status="blocked",
            reason_code="periodic_report_snapshot_not_authorized",
        )
    if not _periodic_entity_matches(entity, snapshot) or not _entity_value_grounded(
        entity, segment_text
    ):
        return _report_decision(
            action,
            segment_id,
            status="blocked",
            reason_code="periodic_report_snapshot_not_authorized",
        )
    if action.action_type == "query_periodic_report":
        return _report_decision(
            action,
            segment_id,
            status="admitted",
            reason_code="periodic_report_read_authorized",
            object_payload=_periodic_object_payload(snapshot),
        )
    if snapshot.status != "collecting":
        return _report_decision(
            action,
            segment_id,
            status="blocked",
            reason_code="periodic_report_not_writable",
        )

    patch: dict[str, Any] = {}
    targets: tuple[str, ...] = ()
    if action.action_type == "capture_report_event":
        field_name = str(entity.attributes.get("field") or "") if entity else ""
        value = str(entity.value or "").strip() if entity else ""
        if (
            field_name not in _PERIODIC_REPORT_FIELDS
            or not value
            or _normalize_reference(value) not in _normalize_reference(segment_text)
        ):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="periodic_report_fact_not_grounded",
            )
        patch = {"field": field_name, "value": value}
    elif action.action_type in {
        "edit_periodic_report_item",
        "delete_periodic_report_item",
    }:
        targets = _entity_target_ids(entity)
        if (
            not targets
            or len(set(targets)) != len(targets)
            or any(target not in _periodic_item_ids(snapshot) for target in targets)
        ):
            return _report_decision(
                action,
                segment_id,
                status="blocked",
                reason_code="periodic_item_target_not_authorized",
            )
        if action.action_type == "edit_periodic_report_item":
            replacement = str(entity.attributes.get("replacement") or "").strip() if entity else ""
            if (
                not replacement
                or _normalize_reference(replacement) not in _normalize_reference(segment_text)
            ):
                return _report_decision(
                    action,
                    segment_id,
                    status="blocked",
                    reason_code="periodic_item_replacement_not_grounded",
                )
            patch = {"replacement": replacement}
    command_type, _ = _REPORT_MUTATION_CONTRACTS[action.action_type]
    authority_scope = {
        "report_type": snapshot.report_type,
        "period_key": snapshot.period_key,
        "report_id": snapshot.report_id,
        "report_version": snapshot.version,
        "command_type": command_type,
        "target_item_ids": list(targets),
        "patch": patch,
    }
    return _report_mutation_admitted(
        action,
        segment_id,
        snapshot,
        authority_scope=authority_scope,
        reason_code="periodic_report_mutation_authorized",
    )


def _report_decision(
    action: RequiredAction,
    segment_id: str,
    *,
    status: str,
    reason_code: str,
    object_payload: Mapping[str, Any] | None = None,
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    return (
        AdmissionDecision(
            action_id=action.action_id,
            segment_id=segment_id,
            domain="report",
            operation=action.action_type,
            status=status,
            reason_code=reason_code,
        ),
        dict(object_payload or {}),
    )


def _report_mutation_admitted(
    action: RequiredAction,
    segment_id: str,
    snapshot: _TrustedDailyReport | _TrustedPeriodicReport,
    *,
    authority_scope: Mapping[str, Any],
    reason_code: str,
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    object_payload = (
        _daily_object_payload(snapshot)
        if isinstance(snapshot, _TrustedDailyReport)
        else _periodic_object_payload(snapshot)
    )
    _, allowed_changed_fields = _REPORT_MUTATION_CONTRACTS[action.action_type]
    object_payload.update(
        {
            "authority_scope": dict(authority_scope),
            "allowed_changed_fields": list(allowed_changed_fields),
        }
    )
    return _report_decision(
        action,
        segment_id,
        status="admitted",
        reason_code=reason_code,
        object_payload=object_payload,
    )


def _daily_command_admitted(
    action: RequiredAction,
    segment_id: str,
    snapshot: _TrustedDailyReport,
    *,
    command_type: str,
    target_item_ids: tuple[str, ...] = (),
    patch: Mapping[str, Any] | None = None,
    reason_code: str,
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    return _report_mutation_admitted(
        action,
        segment_id,
        snapshot,
        authority_scope={
            "report_type": "daily",
            "report_id": snapshot.report_id,
            "report_version": snapshot.version,
            "command_type": command_type,
            "target_item_ids": list(target_item_ids),
            "patch": dict(patch or {}),
        },
        reason_code=reason_code,
    )


def _daily_object_payload(snapshot: _TrustedDailyReport) -> dict[str, Any]:
    return {
        "object_type": "daily_report",
        "report_id": snapshot.report_id,
        "version": snapshot.version,
    }


def _periodic_object_payload(snapshot: _TrustedPeriodicReport) -> dict[str, Any]:
    return {
        "object_type": "periodic_report",
        "report_id": snapshot.report_id,
        "version": snapshot.version,
    }


def _report_object_key(
    object_payload: Mapping[str, Any],
) -> tuple[str, str] | None:
    object_type = str(object_payload.get("object_type") or "")
    report_id = str(object_payload.get("report_id") or "")
    if object_type not in {"daily_report", "periodic_report"} or not report_id:
        return None
    return object_type, report_id


def _rebind_report_object_version(
    object_payload: Mapping[str, Any],
    *,
    version_offsets: Mapping[tuple[str, str], int],
) -> dict[str, Any]:
    rebound = dict(object_payload)
    report_key = _report_object_key(rebound)
    if report_key is None:
        return rebound
    offset = int(version_offsets.get(report_key, 0))
    if not offset:
        return rebound
    raw_version = rebound.get("version")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        return rebound
    rebound["version"] = raw_version + offset
    authority_scope = rebound.get("authority_scope")
    if isinstance(authority_scope, Mapping):
        authority = dict(authority_scope)
        if "report_version" in authority:
            authority["report_version"] = raw_version + offset
        if "version" in authority:
            authority["version"] = raw_version + offset
        rebound["authority_scope"] = authority
    return rebound


def _trusted_daily_report_context(
    resources: Mapping[str, Any],
) -> tuple[_TrustedDailyReport | None, tuple[_TrustedDailyReport, ...]]:
    current_date = _current_daily_report_date(resources)
    draft = _parse_daily_report(resources.get("daily_draft"), report_date=current_date)
    parsed: list[_TrustedDailyReport] = []
    raw_reports = resources.get("daily_reports")
    if isinstance(raw_reports, list):
        for raw in raw_reports:
            report_date = str(raw.get("report_date") or "") if isinstance(raw, dict) else ""
            candidate = _parse_daily_report(raw, report_date=report_date)
            if candidate is not None:
                parsed.append(candidate)
    if draft is not None:
        parsed.append(draft)
    by_id: dict[str, _TrustedDailyReport] = {}
    for snapshot in parsed:
        existing = by_id.get(snapshot.report_id)
        if existing is not None and existing != snapshot:
            return None, ()
        by_id[snapshot.report_id] = snapshot
    current = by_id.get(draft.report_id) if draft is not None else None
    return current, tuple(by_id.values())


def _parse_daily_report(
    raw: Any,
    *,
    report_date: str,
) -> _TrustedDailyReport | None:
    if not isinstance(raw, dict):
        return None
    report_id = str(raw.get("report_id") or "").strip()
    version = raw.get("version")
    status = str(raw.get("status") or "").strip()
    if (
        not report_id
        or not report_date
        or not _is_iso_date(report_date)
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 0
        or status not in {*_DAILY_DRAFT_MUTABLE_STATUSES, "completed"}
    ):
        return None
    sections: dict[str, list[str]] = {field: [] for field in _DAILY_REPORT_FIELDS}
    item_ids: dict[str, list[str]] = {field: [] for field in _DAILY_REPORT_FIELDS}
    raw_items = raw.get("items", [])
    if not isinstance(raw_items, list):
        return None
    seen_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            return None
        item_id = str(item.get("item_id") or "").strip()
        field_name = str(item.get("field") or "").strip()
        text = item.get("text")
        if (
            not item_id
            or item_id in seen_ids
            or field_name not in _DAILY_REPORT_FIELDS
            or not isinstance(text, str)
        ):
            return None
        seen_ids.add(item_id)
        item_ids[field_name].append(item_id)
        sections[field_name].append(text)
    return _TrustedDailyReport(
        report_id=report_id,
        report_date=report_date,
        version=version,
        status=status,
        sections={key: tuple(values) for key, values in sections.items()},
        item_ids={key: tuple(values) for key, values in item_ids.items()},
    )


def _trusted_periodic_report(
    raw: Any,
    *,
    actor_user_id: str,
    occurred_at: datetime,
    timezone_name: str,
) -> _TrustedPeriodicReport | None:
    if not isinstance(raw, dict):
        return None
    report_id = str(raw.get("report_id") or "").strip()
    owner_user_id = str(raw.get("owner_user_id") or "").strip()
    report_type = str(raw.get("report_type") or "").strip()
    period_key = str(raw.get("period_key") or "").strip()
    version = raw.get("version")
    status = str(raw.get("status") or "").strip()
    if (
        not report_id
        or owner_user_id != actor_user_id
        or report_type not in {"weekly", "monthly"}
        or period_key != _current_period_key(report_type, occurred_at, timezone_name)
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 0
        or status not in {"collecting", "completed"}
    ):
        return None
    raw_sections = raw.get("sections")
    raw_item_ids = raw.get("item_ids")
    if not isinstance(raw_sections, dict) or not isinstance(raw_item_ids, dict):
        return None
    sections: dict[str, tuple[str, ...]] = {}
    item_ids: dict[str, tuple[str, ...]] = {}
    seen_ids: set[str] = set()
    for field_name in _PERIODIC_REPORT_FIELDS:
        values = raw_sections.get(field_name, [])
        ids = raw_item_ids.get(field_name, [])
        if (
            not isinstance(values, list)
            or not isinstance(ids, list)
            or len(values) != len(ids)
            or any(not isinstance(value, str) for value in values)
            or any(not isinstance(item_id, str) or not item_id for item_id in ids)
            or seen_ids.intersection(ids)
        ):
            return None
        seen_ids.update(ids)
        sections[field_name] = tuple(values)
        item_ids[field_name] = tuple(ids)
    return _TrustedPeriodicReport(
        report_id=report_id,
        owner_user_id=owner_user_id,
        report_type=report_type,
        period_key=period_key,
        version=version,
        status=status,
        sections=sections,
        item_ids=item_ids,
    )


def _resolve_daily_report(
    entity: ConversationEntity | None,
    snapshots: tuple[_TrustedDailyReport, ...],
    *,
    current: _TrustedDailyReport | None,
) -> _TrustedDailyReport | None:
    if entity is None or entity.entity_type != "daily_report":
        return None
    attributes = entity.attributes
    report_id = str(attributes.get("report_id") or "").strip()
    report_date = str(attributes.get("report_date") or "").strip()
    raw_version = attributes.get("version")
    if raw_version is not None and (
        not isinstance(raw_version, int) or isinstance(raw_version, bool)
    ):
        return None
    if not report_id and not report_date and raw_version is None:
        return current
    matches = tuple(
        snapshot
        for snapshot in snapshots
        if (not report_id or snapshot.report_id == report_id)
        and (not report_date or snapshot.report_date == report_date)
        and (raw_version is None or snapshot.version == raw_version)
    )
    return matches[0] if len(matches) == 1 else None


def _periodic_entity_matches(
    entity: ConversationEntity | None,
    snapshot: _TrustedPeriodicReport,
) -> bool:
    if entity is None:
        return False
    attributes = entity.attributes
    if str(attributes.get("report_type") or "") != snapshot.report_type:
        return False
    if entity.entity_type == "periodic_report":
        return (
            str(attributes.get("report_id") or "") == snapshot.report_id
            and attributes.get("version") == snapshot.version
            and str(attributes.get("period_key") or "") == snapshot.period_key
        )
    return entity.entity_type in {"report_event", "report_item_target"}


def _active_daily_tasks(resources: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw_tasks = resources.get("active_tasks")
    if not isinstance(raw_tasks, list):
        return ()
    return tuple(
        item
        for item in raw_tasks
        if isinstance(item, dict)
        and item.get("workflow") == "daily_report"
        and item.get("status") in _DAILY_DRAFT_MUTABLE_STATUSES
    )


def _entity_target_ids(entity: ConversationEntity | None) -> tuple[str, ...]:
    raw_targets = entity.attributes.get("target_item_ids") if entity else None
    if not isinstance(raw_targets, (list, tuple)):
        return ()
    return tuple(str(value).strip() for value in raw_targets if str(value).strip())


def _daily_item_ids(snapshot: _TrustedDailyReport) -> frozenset[str]:
    return frozenset(item_id for values in snapshot.item_ids.values() for item_id in values)


def _daily_targets_share_field(
    snapshot: _TrustedDailyReport,
    targets: tuple[str, ...],
) -> bool:
    fields = {
        field_name
        for field_name, item_ids in snapshot.item_ids.items()
        if any(target in item_ids for target in targets)
    }
    return len(fields) == 1


def _daily_item_entity_matches_snapshot(
    entity: ConversationEntity | None,
    snapshot: _TrustedDailyReport,
    *,
    field_name: str,
    target_id: str,
) -> bool:
    if entity is None or field_name not in _DAILY_REPORT_FIELDS:
        return False
    try:
        index = snapshot.item_ids[field_name].index(target_id)
        trusted_value = snapshot.sections[field_name][index]
    except (KeyError, IndexError, ValueError):
        return False
    return _normalize_reference(entity.value) == _normalize_reference(trusted_value)


def _daily_capture_contract_reason(
    entity: ConversationEntity | None,
    segment_text: str,
) -> str:
    """Reject a non-assertive clause before it can gain Daily write authority."""

    source = str(segment_text or "").strip()
    if entity is not None and str(entity.value or "").strip():
        # A semantic segment may legitimately contain an affirmative Daily fact
        # plus a separate question.  Judge the exact grounded fact clause so a
        # sibling question cannot erase the positive segment-level outcome.
        source = _daily_fact_target_context(
            source,
            raw_fact=str(entity.value or ""),
        )
    source = _daily_assertion_source(source)
    raw_fact = str(entity.value or "").strip() if entity is not None else ""
    if raw_fact and not _daily_item_polarity_preserved(
        source,
        raw_fact=raw_fact,
    ):
        return "daily_statement_polarity_not_preserved"
    attributes = entity.attributes if entity is not None else {}
    statement_mode = str(attributes.get("statement_mode") or "").strip()
    if statement_mode and statement_mode != "asserted":
        return "daily_statement_not_asserted"
    if not source:
        return "daily_statement_not_grounded"
    if daily_item_is_control_command(source):
        return "daily_control_command_not_report_content"
    if re.search(
        r"(?:不要|不用|别|无需).{0,8}(?:记入|写入|写进|补到|放进).{0,8}日报"
        r"|(?:不要|不用|别|无需).{0,8}日报.{0,8}(?:记|写|补|填)",
        source,
    ):
        return "daily_write_opted_out"
    if re.search(r"[?？]", source) or _non_assertive_framing(source) in {
        "question",
        "hypothetical",
        "quoted",
        "user_opted_out",
    }:
        return "daily_statement_not_asserted"
    explicit_daily_write = bool(
        re.search(
            r"(?:记入|写入|写进|补到|放进).{0,8}日报|日报.{0,8}(?:记|写|补|填)",
            source,
        )
    )
    if explicit_daily_write:
        return ""
    if re.match(
        r"^\s*(?:听说|据说|\S{1,8}(?:说|称|表示|反馈|提到))",
        source,
    ) or re.match(
        r"^\s*(?:会议纪要|邮件|法院文书|正式材料).{0,12}(?:写着|写明|显示|提到|记录|称)",
        source,
    ):
        return "daily_statement_reported_or_quoted"
    field_name = str(attributes.get("field") or "")
    if field_name == "tomorrow_plan" and daily_item_has_termination_state(source):
        return "daily_positive_plan_fact_cancelled"
    if field_name in {"today_work", "tomorrow_plan"} and (
        _DAILY_NONAFFIRMATIVE_LEADING.search(_normalize_reference(source))
    ):
        return "daily_statement_not_affirmative"
    if field_name == "today_work" and re.search(
        r"(?:没|没有|未|尚未|还没|并未).{0,6}"
        r"(?:完成|做完|处理|审核|推进|跟进|整理|制作|参加|提交)",
        source,
    ):
        return "daily_positive_work_fact_negated"
    if (
        field_name == "tomorrow_plan"
        and not daily_item_is_nominal_termination_work(source)
        and re.search(
            r"(?:取消|不去|不再|不用|无需|没法|无法|去不了).{0,16}"
            r"(?:明天|明日|计划|出差|会议|开庭|审核|处理|推进|跟进)"
            r"|(?:明天|明日|计划|出差|会议|开庭).{0,16}"
            r"(?:取消|不去|不再|不用|无需|没法|无法|去不了)",
            source,
        )
    ):
        return "daily_positive_plan_fact_cancelled"
    return ""


def _daily_assertion_source(value: str) -> str:
    """Remove Daily document scaffolding without removing assertion framing."""

    source = str(value or "").strip()
    source = _DAILY_SECTION_ASSERTION_PREFIX.sub("", source, count=1)
    source = _DAILY_ITEM_ASSERTION_PREFIX.sub("", source, count=1)
    return source.strip()


def _daily_item_polarity_preserved(source: str, *, raw_fact: str) -> bool:
    """Reject model items that omit nearby negation or cancellation framing."""

    normalized_source = _normalize_reference(source)
    normalized_fact = _normalize_reference(_daily_assertion_source(raw_fact))
    if not normalized_source or not normalized_fact:
        return False
    starts: list[int] = []
    cursor = 0
    while True:
        start = normalized_source.find(normalized_fact, cursor)
        if start < 0:
            break
        starts.append(start)
        cursor = start + max(1, len(normalized_fact))
    if not starts:
        return False
    for start in starts:
        prefix = normalized_source[:start]
        suffix = normalized_source[start + len(normalized_fact) :]
        if _DAILY_DROPPED_POLARITY_PREFIX.search(prefix):
            return False
        if _DAILY_DROPPED_POLARITY_SUFFIX.search(
            suffix
        ) or daily_item_has_termination_state(suffix):
            return False
    return True


def _daily_section_replacement_grounded(
    entity: ConversationEntity | None,
    snapshot: _TrustedDailyReport,
    *,
    segment_text: str,
    turn_text: str,
) -> bool:
    if entity is None or entity.entity_type != "daily_report":
        return False
    attributes = entity.attributes
    field_name = str(attributes.get("field") or "").strip()
    raw_items = attributes.get("items")
    if (
        str(attributes.get("report_id") or "") != snapshot.report_id
        or attributes.get("version") != snapshot.version
        or field_name not in _DAILY_REPORT_FIELDS
        or not isinstance(raw_items, (list, tuple))
        or not raw_items
        or any(not isinstance(value, str) or not value.strip() for value in raw_items)
        or not _entity_value_grounded(entity, segment_text)
    ):
        return False
    items = tuple(str(value).strip() for value in raw_items)
    if any(
        _daily_capture_contract_reason(
            ConversationEntity(
                entity_id=f"daily-section-item-{index}",
                entity_type="daily_event",
                value=item,
                confidence=1.0,
                attributes={"field": field_name},
            ),
            turn_text,
        )
        for index, item in enumerate(items)
    ):
        return False
    document = parse_structured_daily_document(turn_text)
    if document is not None and set(document.fields) == set(_DAILY_REPORT_FIELDS):
        expected = tuple(
            item.value for item in document.items if item.field == field_name
        )
        return items == expected

    section_items = parse_structured_daily_section(segment_text, field_name)
    return section_items is not None and items == tuple(
        item.value for item in section_items
    )


def _explicit_standalone_daily_fact_authorized(
    *,
    field_name: str,
    segment_text: str,
    raw_fact: str = "",
) -> bool:
    """Authorize only self-identifying Daily facts without an active prompt.

    The semantic model still chooses the facet, but a closed textual contract
    prevents a bare topic, question, hypothetical, or quoted statement from
    gaining report-write authority merely because the model labeled it Daily.
    """

    source = str(segment_text or "").strip()
    if str(raw_fact or "").strip():
        source = _daily_fact_target_context(source, raw_fact=raw_fact)
    if field_name not in _DAILY_REPORT_FIELDS or not source:
        return False
    if _daily_capture_contract_reason(
        ConversationEntity(
            entity_id="standalone-daily-capture-contract",
            entity_type="daily_event",
            value=source,
            confidence=1.0,
            attributes={"field": field_name},
        ),
        source,
    ):
        return False
    explicit_daily_write = bool(
        re.search(
            r"(?:记入|写入|写进|补到|放进).{0,8}日报|日报.{0,8}(?:记|写|补|填)",
            source,
        )
    )
    if re.search(
        r"(?:不要|不用|别|无需).{0,8}(?:记入|写入|写进|补到|放进).{0,8}日报"
        r"|(?:不要|不用|别|无需).{0,8}日报.{0,8}(?:记|写|补|填)",
        source,
    ):
        return False
    if re.search(r"[?？]", source) or daily_item_is_unpunctuated_question(
        _daily_assertion_source(source)
    ):
        return False
    if re.match(
        r"^\s*(?:如果|假如|假设|要是|听说|据说|\S{1,8}(?:说|称|表示|反馈|提到))",
        source,
    ):
        return False
    if re.match(
        r"^\s*(?:会议纪要|邮件|法院文书|正式材料).{0,12}(?:写着|写明|显示|提到|记录|称)",
        source,
    ):
        return False
    framing = _non_assertive_framing(_daily_assertion_source(source))
    if framing and not (framing == "quoted" and explicit_daily_write):
        return False
    if explicit_daily_write:
        return True
    if field_name == "today_work":
        if re.search(
            r"(?:没|没有|未|尚未|还没|并未).{0,6}"
            r"(?:完成|做完|处理|审核|推进|跟进|整理|制作|参加|提交)",
            source,
        ):
            return False
        return bool(
            re.search(r"(?:^|[\n；;])\s*(?:今日|今天)(?:工作|完成)\s*[：:]", source)
            or re.search(
                r"^(?:我)?(?:今天|今日).{0,24}(?:完成|做了|处理|审核|推进|跟进|整理|制作|参加|沟通|开会|开庭|提交|梳理|评估|复盘|出差)",
                source,
            )
            or re.search(
                r"^(?:我)?(?:已经|已|完成了|做完了|审核了|处理了|推进了|跟进了|整理了|制作了|参加了|提交了)",
                source,
            )
            or re.search(
                r"^[^\n；;？?]{1,32}(?:已经|已)"
                r"(?:完成|做完|处理|审核|推进|跟进|整理|制作|参加|提交|梳理|评估|复盘)",
                source,
            )
        )
    if field_name == "problems":
        return bool(
            re.search(
                r"(?:^|[\n；;])\s*(?:问题(?:与|和|/)?风险|风险(?:与|和|/)?问题|问题)"
                r"(?:就是|是|为|\s*[，,:：])",
                source,
            )
            or re.search(r"(?:存在|遇到|当前|仍有).{0,12}(?:问题|风险|阻塞|故障|延误)", source)
        )
    if (
        not daily_item_is_nominal_termination_work(
            _daily_assertion_source(source)
        )
        and re.search(
            r"(?:取消|不去|不再|不用|无需|没法|无法|去不了).{0,16}"
            r"(?:明天|明日|计划|出差|会议|开庭|审核|处理|推进|跟进)"
            r"|(?:明天|明日).{0,16}(?:取消|不去|不再|不用|无需|没法|无法|去不了)",
            source,
        )
    ):
        return False
    return bool(
        re.search(
            r"(?:^|[\n；;])\s*(?:明日|明天)(?:工作)?计划\s*[：:]",
            source,
        )
        or re.search(r"这是(?:明天|明日)(?:的)?(?:工作|计划|工作计划)", source)
        or (
            re.search(r"(?:明天|明日)", source)
            and re.search(
                r"(?:计划|准备|参加|出差|开庭|审核|处理|推进|跟进|整理|制作|提交|沟通|会议|待办|工作|评估|复盘)",
                source,
            )
        )
    )


def _periodic_item_ids(snapshot: _TrustedPeriodicReport) -> frozenset[str]:
    return frozenset(item_id for values in snapshot.item_ids.values() for item_id in values)


def _entity_value_grounded(
    entity: ConversationEntity | None,
    segment_text: str,
) -> bool:
    if entity is None:
        return False
    value = _normalize_reference(entity.value)
    return bool(value) and value in _normalize_reference(segment_text)


def _daily_reference_grounded(
    entity: ConversationEntity | None,
    snapshot: _TrustedDailyReport,
    segment_text: str,
) -> bool:
    if entity is None:
        return False
    attributes = entity.attributes
    if (
        str(attributes.get("report_id") or "") == snapshot.report_id
        and attributes.get("version") == snapshot.version
        and str(attributes.get("report_date") or "") == snapshot.report_date
    ):
        return True
    return _entity_value_grounded(entity, segment_text)


def _daily_mutation_reference_grounded(
    entity: ConversationEntity,
    snapshot: _TrustedDailyReport,
    segment_text: str,
    *,
    occurred_at: datetime,
    timezone_name: str,
) -> bool:
    """Validate a model-proposed report target against the exact user segment.

    Snapshot identifiers are trusted resources, but a model choosing one of them
    is not authority.  A mutation target therefore also needs a user-visible
    report-date expression in the same action segment.
    """

    attributes = entity.attributes
    if (
        str(attributes.get("report_id") or "") != snapshot.report_id
        or attributes.get("version") != snapshot.version
        or str(attributes.get("report_date") or "") != snapshot.report_date
    ):
        return False
    referenced_dates = _explicit_daily_report_target_dates(
        segment_text,
        occurred_at=occurred_at,
        timezone_name=timezone_name,
    )
    return referenced_dates == frozenset({snapshot.report_date})


def _daily_fact_target_context(segment_text: str, *, raw_fact: str) -> str:
    """Keep a report target bound to the clause containing this exact fact."""

    fact = _normalize_reference(raw_fact)
    if not fact:
        return segment_text
    clauses = tuple(
        clause.strip()
        for clause in re.split(r"[\uff1b;\u3002\uff01!\uff1f?\n]+", segment_text)
        if clause.strip()
    )
    matches = tuple(
        clause for clause in clauses if fact in _normalize_reference(clause)
    )
    return matches[0] if len(matches) == 1 else segment_text


def _explicit_daily_report_target_dates(
    segment_text: str,
    *,
    occurred_at: datetime,
    timezone_name: str,
) -> frozenset[str]:
    try:
        local_date = occurred_at.astimezone(ZoneInfo(timezone_name)).date()
    except (KeyError, ValueError):
        return frozenset()

    dates: set[str] = set()
    relative_offsets = {
        "\u4eca\u5929": 0,
        "\u4eca\u65e5": 0,
        "\u6628\u5929": -1,
        "\u6628\u65e5": -1,
        "\u524d\u5929": -2,
    }
    relative_pattern = re.compile(
        r"(?P<label>\u4eca\u5929|\u4eca\u65e5|\u6628\u5929|\u6628\u65e5|\u524d\u5929)\s*\u7684?\s*\u65e5\u62a5"
    )
    for match in relative_pattern.finditer(segment_text):
        dates.add(
            (local_date + timedelta(days=relative_offsets[match.group("label")])).isoformat()
        )

    iso_pattern = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})\s*\u7684?\s*\u65e5\u62a5")
    for match in iso_pattern.finditer(segment_text):
        if _is_iso_date(match.group("date")):
            dates.add(match.group("date"))

    chinese_date_pattern = re.compile(
        r"(?:(?P<year>\d{4})\u5e74)?(?P<month>\d{1,2})\u6708(?P<day>\d{1,2})\u65e5\s*\u7684?\s*\u65e5\u62a5"
    )
    for match in chinese_date_pattern.finditer(segment_text):
        try:
            parsed = date(
                int(match.group("year") or local_date.year),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            continue
        dates.add(parsed.isoformat())
    return frozenset(dates)


def _historical_daily_mutation_allowed(resources: Mapping[str, Any]) -> bool:
    policy = resources.get("daily_policy")
    return isinstance(policy, Mapping) and policy.get("historical_mutation_allowed") is True


def _current_daily_report_date(resources: Mapping[str, Any]) -> str:
    policy = resources.get("daily_policy")
    if isinstance(policy, dict):
        value = str(policy.get("current_report_date") or "").strip()
        if _is_iso_date(value):
            return value
    draft = resources.get("daily_draft")
    if isinstance(draft, dict):
        value = str(draft.get("report_date") or "").strip()
        if _is_iso_date(value):
            return value
    return ""


def _current_period_key(
    report_type: str,
    occurred_at: datetime,
    timezone_name: str,
) -> str:
    try:
        local_date = occurred_at.astimezone(ZoneInfo(timezone_name)).date()
    except (KeyError, ValueError):
        return ""
    if report_type == "weekly":
        iso_year, iso_week, _ = local_date.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if report_type == "monthly":
        return f"{local_date.year:04d}-{local_date.month:02d}"
    return ""


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _project_current_work_to_tomorrow(value: str) -> str:
    original = str(value or "").strip()
    if not original or not re.search(r"[\u4e00-\u9fff]", original):
        return original
    clean = re.sub(r"^(?:今天|今日)(?:上午|下午|晚上)?[，,:：\s]*", "", original).strip()
    if clean.startswith("继续"):
        return clean
    completed = re.match(r"^(?:已经|已)?完成(?:了)?(.+)$", clean)
    if completed:
        return f"继续推进{completed.group(1).strip()}"
    clean = re.sub(r"^(?:已经|已)?开始", "", clean).strip()
    clean = re.sub(
        r"^(优化|处理|办理|推进|搭建|整理|联系|沟通|提交|审核|核对|准备|跟进|协调|参加)了(?=.+)",
        r"\1",
        clean,
    )
    return f"继续{clean}" if clean else original


def _action_contract(action: RequiredAction) -> tuple[str, str]:
    if action.action_type in _REPORT_ACTIONS:
        return "report", action.action_type
    return {
        "record_case_progress": ("case", "record_case_progress"),
        "update_case_progress": ("case", "update_case_progress"),
        "delete_case_progress": ("case", "delete_case_progress"),
        "link_case_progress": ("case", "link_case_progress"),
        "answer_case_query": ("case", "answer_case_query"),
        "query_case_progress": ("case", "query_case_progress"),
        "query_operation_status": ("runtime", "query_operation_status"),
        "search_enterprise_knowledge": ("knowledge", "search_enterprise_knowledge"),
        "record_travel_event": ("travel", "record_travel_event"),
        "update_travel_event": ("travel", "update_travel_event"),
        "respond_travel_collaboration": (
            "travel",
            "respond_travel_collaboration",
        ),
        "update_case_followup_policy": (
            "case",
            "update_case_followup_policy",
        ),
        "trigger_case_followup_now": (
            "case",
            "trigger_case_followup_now",
        ),
    }.get(action.action_type, ("runtime", action.action_type))


def _action_requires_execution_ticket(action_type: str) -> bool:
    return action_type not in _READ_ONLY_ACTIONS


def _capture_decision_audit_artifacts(
    *,
    decisions: tuple[AdmissionDecision, ...],
    policy: AdmissionCapturePolicy,
    created_at: datetime,
) -> tuple[tuple[SemanticReviewItem, ...], tuple[DeferredSemanticEvent, ...]]:
    """Project finalized Decisions into non-authoritative, digest-only artifacts."""

    review_items: list[SemanticReviewItem] = []
    deferred_events: list[DeferredSemanticEvent] = []
    for decision in decisions:
        candidate_snapshot = {
            "action_id": decision.action_id,
            "decision_verdict": decision.status,
            "evidence_refs": decision.evidence_refs,
        }
        capture_review = policy.review_enabled and (
            (
                policy.mode == "shadow"
                and decision.status != "deferred_audit_only"
            )
            or (
                policy.mode != "shadow"
                and decision.status == "review_only"
            )
        )
        if capture_review:
            review_id = _stable_uuid(
                "semantic-review",
                decision.trace_id,
                decision.decision_id,
                decision.status,
                policy.mode,
            )
            review_items.append(
                SemanticReviewItem(
                    review_id=review_id,
                    trace_id=decision.trace_id,
                    decision_id=decision.decision_id,
                    tenant_id=decision.tenant_id,
                    user_id=decision.user_id,
                    conversation_id=decision.conversation_id,
                    source_turn_id=decision.source_turn_id,
                    source_message_id=decision.source_message_id,
                    segment_id=decision.segment_id,
                    segment_text_sha256=decision.segment_text_sha256,
                    segment_start_offset=decision.segment_start_offset,
                    segment_end_offset=decision.segment_end_offset,
                    domain=decision.domain,
                    operation=decision.operation,
                    object_ref=decision.object_ref,
                    reason_code=decision.reason_code,
                    candidate_snapshot=candidate_snapshot,
                    resolution={},
                    idempotency_key=_stable_digest(
                        "semantic-review",
                        decision.tenant_id,
                        review_id,
                    ),
                    created_at=created_at,
                )
            )
        if policy.deferred_enabled and decision.status == "deferred_audit_only":
            deferred_event_id = _stable_uuid(
                "deferred-semantic-event",
                decision.trace_id,
                decision.decision_id,
                decision.status,
            )
            deferred_events.append(
                DeferredSemanticEvent(
                    deferred_event_id=deferred_event_id,
                    trace_id=decision.trace_id,
                    decision_id=decision.decision_id,
                    tenant_id=decision.tenant_id,
                    user_id=decision.user_id,
                    conversation_id=decision.conversation_id,
                    source_turn_id=decision.source_turn_id,
                    source_message_id=decision.source_message_id,
                    segment_id=decision.segment_id,
                    segment_text_sha256=decision.segment_text_sha256,
                    segment_start_offset=decision.segment_start_offset,
                    segment_end_offset=decision.segment_end_offset,
                    domain=decision.domain,
                    operation=decision.operation,
                    object_ref=decision.object_ref,
                    reason_code=decision.reason_code,
                    payload=candidate_snapshot,
                    not_before=None,
                    expires_at=None,
                    idempotency_key=_stable_digest(
                        "deferred-semantic-event",
                        decision.tenant_id,
                        deferred_event_id,
                    ),
                    created_at=created_at,
                )
            )
    return tuple(review_items), tuple(deferred_events)


def _ground_action_segment(
    *,
    turn: CognitiveTurn,
    action: RequiredAction,
    segment: SemanticSegment | None,
    bound_segment_count: int,
) -> tuple[SemanticSegment | None, str]:
    if bound_segment_count == 0:
        return None, "action_segment_missing"
    if bound_segment_count != 1 or segment is None:
        return None, "action_segment_ambiguous"
    if action.intent not in segment.intents or not set(action.entity_ids).issubset(
        segment.entity_ids
    ):
        return None, "action_segment_binding_mismatch"
    if hashlib.sha256(segment.text.encode("utf-8")).hexdigest() != segment.text_hash:
        return None, "segment_hash_mismatch"
    if segment.start_offset >= 0 or segment.end_offset >= 0:
        if (
            segment.start_offset < 0
            or segment.end_offset < segment.start_offset
            or turn.text[segment.start_offset : segment.end_offset] != segment.text
        ):
            return None, "segment_not_grounded_in_turn"
        return segment, ""
    offsets = _substring_offsets(turn.text, segment.text)
    if len(offsets) != 1:
        return None, "segment_not_grounded_in_turn"
    start_offset = offsets[0]
    return (
        replace(
            segment,
            start_offset=start_offset,
            end_offset=start_offset + len(segment.text),
        ),
        "",
    )


def _substring_offsets(source: str, fragment: str) -> tuple[int, ...]:
    if not fragment:
        return ()
    offsets: list[int] = []
    start = 0
    while True:
        index = source.find(fragment, start)
        if index < 0:
            break
        offsets.append(index)
        start = index + 1
    return tuple(offsets)


def _trusted_scope(turn: CognitiveTurn, state: ConversationState) -> dict[str, str]:
    tenant_id = str(turn.tenant_id or "").strip()
    user_id = str(turn.actor_user_id or turn.user_id or "").strip()
    if not tenant_id or not user_id:
        raise ValueError("verified tenant and user scope are required")
    if state.conversation_id != turn.conversation_id or state.user_id != turn.user_id:
        raise ValueError("conversation state does not match turn")
    return {"tenant_id": tenant_id, "user_id": user_id}


def _verified_read_access(turn: CognitiveTurn, raw_access: Any) -> bool:
    if not isinstance(raw_access, Mapping) or raw_access.get("allowed") is not True:
        return False
    return (
        str(raw_access.get("tenant_id") or "") == str(turn.tenant_id or "")
        and str(raw_access.get("user_id") or "")
        == str(turn.actor_user_id or turn.user_id or "")
    )


def _single_entity(
    action: RequiredAction,
    entity_by_id: Mapping[str, ConversationEntity],
) -> ConversationEntity | None:
    if len(action.entity_ids) != 1:
        return None
    return entity_by_id.get(action.entity_ids[0])


def _decide_case_progress_mutation(
    *,
    turn: CognitiveTurn,
    action: RequiredAction,
    segment_id: str,
    segment_text: str,
    entity_by_id: Mapping[str, ConversationEntity],
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    entity = _single_entity(action, entity_by_id)
    operation = action.action_type
    if entity is None or entity.entity_type != "case_progress_ref":
        return _blocked_decision(
            action, segment_id, "case", operation,
            "case_progress_reference_required",
        )
    attributes = entity.attributes
    progress = _resolve_recent_case_progress(
        attributes=attributes,
        resources=turn.resources,
        segment_text=segment_text,
    )
    if progress is None:
        return _blocked_decision(
            action, segment_id, "case", operation,
            "case_progress_not_uniquely_authorized",
        )
    progress_id = str(progress.get("progress_id") or "")
    case_id = str(progress.get("case_id") or "")
    version = int(progress.get("version") or 0)
    authority_scope: dict[str, Any] = {
        "progress_id": progress_id,
        "case_id": case_id,
        "version": version,
        "raw_fact": segment_text,
    }
    allowed_fields: tuple[str, ...]
    if operation == "update_case_progress":
        replacements = {
            "replacement_summary": str(
                attributes.get("replacement_summary") or ""
            ).strip(),
            "replacement_details": str(
                attributes.get("replacement_details") or ""
            ).strip(),
        }
        grounded = {
            name: value
            for name, value in replacements.items()
            if value
            and _normalize_reference(value) in _normalize_reference(segment_text)
        }
        if not grounded or any(
            value and name not in grounded for name, value in replacements.items()
        ):
            return _blocked_decision(
                action, segment_id, "case", operation,
                "case_progress_replacement_not_grounded",
            )
        authority_scope.update(grounded)
        allowed_fields = tuple(
            field
            for field, attribute in (
                ("summary", "replacement_summary"),
                ("details", "replacement_details"),
            )
            if attribute in grounded
        )
    elif operation == "delete_case_progress":
        reason = str(attributes.get("delete_reason") or "").strip()
        if (
            not reason
            or _normalize_reference(reason)
            not in _normalize_reference(segment_text)
        ):
            return _blocked_decision(
                action, segment_id, "case", operation,
                "case_progress_delete_reason_not_grounded",
            )
        authority_scope["delete_reason"] = reason
        allowed_fields = ("deleted_at", "deleted_by", "delete_reason")
    else:
        trusted_fields = (
            ("related_party_ids", "visible_party_ids"),
            ("related_document_ids", "visible_document_ids"),
            ("related_travel_intent_ids", "visible_travel_intent_ids"),
        )
        selected: dict[str, tuple[str, ...]] = {}
        for field, resource_name in trusted_fields:
            raw_ids = attributes.get(field)
            if raw_ids is None:
                continue
            if not isinstance(raw_ids, (list, tuple)):
                return _blocked_decision(
                    action, segment_id, "case", operation,
                    "case_progress_link_target_not_trusted",
                )
            values = tuple(str(value) for value in raw_ids if str(value).strip())
            trusted = {
                str(value)
                for value in (turn.resources.get(resource_name) or ())
                if str(value).strip()
            }
            if not values or not set(values).issubset(trusted):
                return _blocked_decision(
                    action, segment_id, "case", operation,
                    "case_progress_link_target_not_trusted",
                )
            selected[field] = values
        if not selected:
            return _blocked_decision(
                action, segment_id, "case", operation,
                "case_progress_link_target_required",
            )
        authority_scope.update(selected)
        allowed_fields = tuple(selected)
    return (
        AdmissionDecision(
            action_id=action.action_id,
            segment_id=segment_id,
            domain="case",
            operation=operation,
            status="admitted",
            reason_code="case_progress_target_and_change_authorized",
        ),
        {
            "object_type": "case_progress",
            "stable_id": progress_id,
            "version": version,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_fields,
        },
    )


def _resolve_recent_case_progress(
    *,
    attributes: Mapping[str, Any],
    resources: Mapping[str, Any],
    segment_text: str,
) -> Mapping[str, Any] | None:
    raw_progress = resources.get("recent_case_progress")
    raw_cases = resources.get("visible_cases")
    if not isinstance(raw_progress, list) or not isinstance(raw_cases, list):
        return None
    visible_case_ids = {
        str(item.get("case_id") or "")
        for item in raw_cases
        if isinstance(item, Mapping) and str(item.get("case_id") or "")
    }
    candidates = [
        item
        for item in raw_progress
        if isinstance(item, Mapping)
        and str(item.get("progress_id") or "")
        and str(item.get("case_id") or "") in visible_case_ids
        and int(item.get("version") or 0) > 0
    ]
    progress_id = str(attributes.get("progress_id") or "").strip()
    if progress_id:
        candidates = [
            item for item in candidates
            if str(item.get("progress_id") or "") == progress_id
        ]
    case_hint = str(attributes.get("case_hint") or "").strip()
    if case_hint:
        if _normalize_reference(case_hint) not in _normalize_reference(segment_text):
            return None
        case = _resolve_visible_case(case_hint, raw_cases)
        if case is None:
            return None
        candidates = [
            item for item in candidates
            if str(item.get("case_id") or "") == str(case.get("case_id") or "")
        ]
    if len(candidates) != 1:
        return None
    expected_version = attributes.get("expected_version")
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version != int(candidates[0].get("version") or 0)
    ):
        return None
    return candidates[0]


def _blocked_decision(
    action: RequiredAction,
    segment_id: str,
    domain: str,
    operation: str,
    reason_code: str,
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    return (
        AdmissionDecision(
            action_id=action.action_id,
            segment_id=segment_id,
            domain=domain,
            operation=operation,
            status="blocked",
            reason_code=reason_code,
        ),
        {},
    )


def _decide_case_followup_mutation(
    *,
    turn: CognitiveTurn,
    action: RequiredAction,
    segment_id: str,
    segment_text: str,
    entity_by_id: Mapping[str, ConversationEntity],
) -> tuple[AdmissionDecision, Mapping[str, Any]]:
    operation = action.action_type
    entity = _single_entity(action, entity_by_id)
    if entity is None or entity.entity_type != "case_followup_policy":
        return _blocked_decision(
            action, segment_id, "case", operation,
            "case_followup_policy_entity_required",
        )
    attributes = entity.attributes
    case_hint = str(attributes.get("case_hint") or entity.value or "").strip()
    if (
        not case_hint
        or _normalize_reference(case_hint) not in _normalize_reference(segment_text)
        or not _grounded_evidence_fragments(
            attributes.get("evidence_spans"), segment_text
        )
    ):
        return _blocked_decision(
            action, segment_id, "case", operation,
            "case_followup_change_not_grounded",
        )
    case = _resolve_visible_case(case_hint, turn.resources.get("visible_cases"))
    if case is None:
        return _blocked_decision(
            action, segment_id, "case", operation,
            "case_followup_case_not_uniquely_authorized",
        )
    case_id = str(case.get("case_id") or "")
    raw_policies = turn.resources.get("active_case_followup_policies")
    actor_user_id = str(turn.actor_user_id or turn.user_id or "")
    policies = [
        item
        for item in raw_policies
        if isinstance(raw_policies, list)
        and isinstance(item, Mapping)
        and str(item.get("case_id") or "") == case_id
        and str(item.get("assigned_user_id") or "") == actor_user_id
        and isinstance(item.get("version"), int)
        and not isinstance(item.get("version"), bool)
        and int(item.get("version") or 0) >= 0
    ] if isinstance(raw_policies, list) else []
    if len(policies) != 1:
        return _blocked_decision(
            action, segment_id, "case", operation,
            "case_followup_policy_not_uniquely_authorized",
        )
    policy = policies[0]
    version = int(policy.get("version") or 0)
    authority_scope: dict[str, Any] = {
        "case_id": case_id,
        "case_version": int(case.get("version") or 0),
        "assigned_user_id": actor_user_id,
        "policy_version": version,
        "raw_fact": segment_text,
    }
    if operation == "trigger_case_followup_now":
        if not _followup_now_grounded(segment_text):
            return _blocked_decision(
                action, segment_id, "case", operation,
                "case_followup_trigger_not_grounded",
            )
        allowed_fields = ("followup_task", "notification_outbox")
    else:
        mutable_fields = (
            "cadence_type",
            "custom_interval_days",
            "snoozed_until",
            "enabled",
            "hearing_reminders_enabled",
            "stage_transition_enabled",
            "node_transition_enabled",
        )
        supplied = {
            name: attributes.get(name)
            for name in mutable_fields
            if name in attributes
        }
        if not supplied or not _followup_policy_fields_grounded(
            supplied, segment_text
        ):
            return _blocked_decision(
                action, segment_id, "case", operation,
                "case_followup_policy_fields_not_grounded",
            )
        authority_scope.update(supplied)
        allowed_fields = tuple(supplied)
    return (
        AdmissionDecision(
            action_id=action.action_id,
            segment_id=segment_id,
            domain="case",
            operation=operation,
            status="admitted",
            reason_code="case_followup_target_and_change_authorized",
        ),
        {
            "object_type": "case_followup_policy",
            "stable_id": case_id,
            "version": version,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_fields,
        },
    )


def _followup_now_grounded(segment_text: str) -> bool:
    normalized = _normalize_reference(segment_text)
    return any(
        token in normalized
        for token in (
            "now",
            "immediately",
            "\u73b0\u5728",
            "\u7acb\u5373",
            "\u9a6c\u4e0a",
        )
    )


def _followup_policy_fields_grounded(
    supplied: Mapping[str, Any], segment_text: str
) -> bool:
    normalized = _normalize_reference(segment_text)
    cadence = supplied.get("cadence_type")
    cadence_tokens = {
        "daily": ("daily", "\u6bcf\u5929"),
        "weekly": ("weekly", "\u6bcf\u5468", "\u4e00\u5468"),
        "every_15_days": ("15days", "\u534a\u4e2a\u6708", "\u5341\u4e94\u5929"),
        "monthly": ("monthly", "\u6bcf\u6708", "\u4e00\u4e2a\u6708"),
        "custom_interval": ("every", "\u6bcf\u9694"),
        "event_only": ("eventonly", "\u53ea\u5728", "\u5173\u952e\u8282\u70b9"),
        "manual_only": ("manualonly", "\u53ea\u4eba\u5de5"),
        "paused": ("paused", "\u6682\u505c", "\u522b\u518d\u95ee"),
        "disabled": ("disabled", "\u5173\u95ed", "\u4e0d\u518d\u4e3b\u52a8\u8ffd\u95ee"),
    }
    if cadence is not None and not any(
        _normalize_reference(token) in normalized
        for token in cadence_tokens.get(str(cadence), ())
    ):
        return False
    custom_days = supplied.get("custom_interval_days")
    if custom_days is not None and str(custom_days) not in segment_text:
        return False
    snoozed_until = supplied.get("snoozed_until")
    if snoozed_until is not None and not any(
        token in normalized
        for token in (
            _normalize_reference(snoozed_until),
            "\u660e\u5929",
            "\u4e0b\u5468",
            "\u6708\u5e95",
        )
    ):
        return False
    enabled = supplied.get("enabled")
    if enabled is False and not any(
        token in normalized
        for token in ("disable", "paused", "\u5173\u95ed", "\u6682\u505c", "\u522b\u518d\u95ee")
    ):
        return False
    reminder_tokens = {
        "hearing_reminders_enabled": ("hearing", "\u5f00\u5ead"),
        "stage_transition_enabled": ("stage", "\u9636\u6bb5"),
        "node_transition_enabled": ("node", "\u8282\u70b9"),
    }
    for field, tokens in reminder_tokens.items():
        if field in supplied and not any(
            _normalize_reference(token) in normalized for token in tokens
        ):
            return False
    return True


def _matches_report_no_item_answer(
    entity: ConversationEntity | None,
    active_task: Mapping[str, Any],
) -> bool:
    if entity is None:
        return False
    metadata = active_task.get("metadata")
    if not isinstance(metadata, dict):
        return False
    if str(metadata.get("expected_field") or "") != str(entity.attributes.get("field") or ""):
        return False
    answer_forms = metadata.get("acceptable_no_item_answers")
    if not isinstance(answer_forms, list):
        return False
    answer = _normalize_reference(entity.value)
    return bool(answer) and answer in {
        _normalize_reference(value) for value in answer_forms if str(value or "").strip()
    }


def _resolve_visible_case(value: str, raw_cases: Any) -> Mapping[str, Any] | None:
    matches = _matching_visible_cases(value, raw_cases)
    return matches[0] if len(matches) == 1 else None


def _matching_visible_cases(
    value: str,
    raw_cases: Any,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(raw_cases, list):
        return ()
    needle = _normalize_reference(value)
    if not needle:
        return ()
    matches: list[Mapping[str, Any]] = []
    for item in raw_cases:
        if not isinstance(item, dict) or not str(item.get("case_id") or ""):
            continue
        labels = [
            item.get("case_id"),
            item.get("case_number"),
            item.get("case_name"),
            item.get("external_case_id"),
            *(item.get("confirmed_aliases") or []),
        ]
        if any(_normalize_reference(label) == needle for label in labels):
            matches.append(item)
    return tuple(matches)


def _matching_visible_cases_from_segment(
    segment_text: str,
    raw_cases: Any,
    *,
    grounded_reference: str = "",
) -> tuple[Mapping[str, Any], ...]:
    """Resolve only trusted case labels that occur in the exact action segment.

    Longer overlapping labels shadow their shorter aliases (``云璟府案`` over
    ``云璟府``), while two independent references or one shared maximal alias
    remain ambiguous.  The model's normalized entity value is never authority.
    """

    if not isinstance(raw_cases, list):
        return ()
    haystack = _normalize_reference(segment_text)
    if not haystack:
        return ()
    occurrences: set[tuple[int, int, str]] = set()
    case_by_id: dict[str, Mapping[str, Any]] = {}
    for item in raw_cases:
        if not isinstance(item, Mapping):
            continue
        case_id = str(item.get("case_id") or "").strip()
        if not case_id:
            continue
        case_by_id[case_id] = item
        aliases = item.get("confirmed_aliases")
        labels = (
            item.get("case_number"),
            item.get("external_case_id"),
            item.get("case_name"),
            *(aliases if isinstance(aliases, (list, tuple)) else ()),
        )
        for raw_label in labels:
            label = _normalize_reference(raw_label)
            if not label:
                continue
            start = haystack.find(label)
            while start >= 0:
                occurrences.add((start, start + len(label), case_id))
                start = haystack.find(label, start + 1)
    if not occurrences:
        grounded_matches = tuple(
            match.case
            for match in match_grounded_visible_cases(
                grounded_reference,
                segment_text=segment_text,
                raw_cases=raw_cases,
            )
        )
        if grounded_matches:
            return grounded_matches
        discovered_matches = discover_visible_case_references(
            segment_text,
            raw_cases,
        )
        if not discovered_matches:
            return ()
        discovered_cases = tuple(match.case for match in discovered_matches)
        if len(discovered_cases) > 1:
            return discovered_cases
        proposed_exact_matches = _matching_visible_cases(
            grounded_reference,
            raw_cases,
        )
        discovered_id = str(discovered_cases[0].get("case_id") or "")
        if len(proposed_exact_matches) != 1 or str(
            proposed_exact_matches[0].get("case_id") or ""
        ) != discovered_id:
            return ()
        return discovered_cases
    maximal = tuple(
        occurrence
        for occurrence in occurrences
        if not any(
            other_start <= occurrence[0]
            and other_end >= occurrence[1]
            and (other_end - other_start) > (occurrence[1] - occurrence[0])
            for other_start, other_end, _ in occurrences
        )
    )
    matched_ids = {case_id for _, _, case_id in maximal}
    return tuple(
        case_by_id[case_id]
        for case_id in case_by_id
        if case_id in matched_ids
    )


def evaluate_assertion_polarity_contract(
    *,
    domain: str,
    segment_text: str,
    statement_mode: str,
    evidence_fragments: tuple[str, ...],
    claim_anchors: tuple[str, ...] = (),
) -> AssertionPolarityAssessment:
    """Validate, but never infer, a mutation assertion from its exact segment.

    The semantic model still proposes the domain and facts.  This contract only
    verifies that the proposal is a direct affirmative assertion; it cannot
    create an action or upgrade a non-assertive proposal into a write.
    """

    if domain not in {"case", "travel"}:
        return AssertionPolarityAssessment(
            classification="unsupported_domain",
            authorizes_mutation=False,
            reason_code="assertion_polarity_domain_unsupported",
        )
    if statement_mode != "asserted":
        return AssertionPolarityAssessment(
            classification="non_asserted_mode",
            authorizes_mutation=False,
            reason_code=f"{domain}_statement_not_asserted",
        )
    if not segment_text.strip() or not evidence_fragments or any(
        not fragment.strip() or fragment not in segment_text
        for fragment in evidence_fragments
    ):
        return AssertionPolarityAssessment(
            classification="ungrounded",
            authorizes_mutation=False,
            reason_code=f"{domain}_evidence_not_grounded",
        )

    framing = _non_assertive_framing(segment_text)
    if framing:
        return AssertionPolarityAssessment(
            classification=framing,
            authorizes_mutation=False,
            reason_code=f"{domain}_assertion_not_direct",
        )

    relevant_clauses = _assertion_relevant_clauses(
        segment_text=segment_text,
        evidence_fragments=evidence_fragments,
        claim_anchors=claim_anchors,
    )
    clause_polarities = tuple(
        _domain_clause_polarity(domain, clause) for clause in relevant_clauses
    )
    if "negative" in clause_polarities and "affirmative" in clause_polarities:
        return AssertionPolarityAssessment(
            classification="ambiguous_polarity",
            authorizes_mutation=False,
            reason_code=f"{domain}_assertion_polarity_ambiguous",
        )
    if "negative" in clause_polarities:
        return AssertionPolarityAssessment(
            classification="negated_or_absent",
            authorizes_mutation=False,
            reason_code=(
                "travel_assertion_negated_or_cancelled"
                if domain == "travel"
                else "case_assertion_negated_or_absent"
            ),
        )
    return AssertionPolarityAssessment(
        classification="affirmative",
        authorizes_mutation=True,
    )


def _non_assertive_framing(segment_text: str) -> str:
    normalized = _normalize_reference(segment_text)
    question_normalized = daily_semantic_detection_copy(segment_text).casefold()
    if any(
        marker in normalized
        for marker in (
            "\u53ea\u662f\u4e3e\u4f8b",
            "\u4e3e\u4e2a\u4f8b\u5b50",
            "\u4f8b\u5982",
            "\u6bd4\u5982",
            "\u5047\u8bbe",
            "\u5047\u5982",
            "\u5982\u679c",
            "\u5018\u82e5",
            "\u8981\u662f",
        )
    ):
        return "hypothetical"
    if any(
        marker in normalized
        for marker in (
            "\u4e0d\u8981\u8bb0\u5f55",
            "\u4e0d\u7528\u8bb0\u5f55",
            "\u522b\u8bb0\u5f55",
            "\u4e0d\u8981\u5199\u5165",
            "\u522b\u5199\u5165",
            "\u53ea\u8bb0\u6848\u4ef6",
        )
    ):
        return "user_opted_out"
    if re.search(r"[\u201c\u2018\"].+?[\u201d\u2019\"]", segment_text):
        return "quoted"
    if (
        "?" in segment_text
        or "\uff1f" in segment_text
        or daily_item_is_unpunctuated_question(question_normalized)
    ):
        return "question"
    return ""


def _assertion_relevant_clauses(
    *,
    segment_text: str,
    evidence_fragments: tuple[str, ...],
    claim_anchors: tuple[str, ...],
) -> tuple[str, ...]:
    clauses = tuple(
        clause.strip()
        for clause in re.split(
            r"[\uff0c,\uff1b;\u3002\uff01!\uff1f?\n]+|(?:\u4f46\u662f|\u4e0d\u8fc7|\u800c\u662f)",
            segment_text,
        )
        if clause.strip()
    )
    normalized_anchors = tuple(
        normalized
        for value in claim_anchors
        if (normalized := _normalize_reference(value))
    )
    anchored = tuple(
        clause
        for clause in clauses
        if any(anchor in _normalize_reference(clause) for anchor in normalized_anchors)
    )
    if anchored:
        return anchored
    evidence_clauses = tuple(
        clause.strip()
        for fragment in evidence_fragments
        for clause in re.split(
            r"[\uff0c,\uff1b;\u3002\uff01!\uff1f?\n]+|(?:\u4f46\u662f|\u4e0d\u8fc7|\u800c\u662f)",
            fragment,
        )
        if clause.strip()
    )
    return evidence_clauses or (segment_text,)


def _domain_clause_polarity(domain: str, clause: str) -> str:
    normalized = _normalize_reference(clause)
    normalized = re.sub(
        r"(?:\u5e76\u4e0d\u662f|\u4e0d\u662f|\u5e76\u975e)(?:\u6ca1\u6709|\u8fd8\u6ca1|\u5c1a\u672a|\u672a\u66fe|\u6ca1|\u672a|\u4e0d)",
        "\u5df2\u7ecf",
        normalized,
    )
    normalized = re.sub(r"(?:\u4e0d\u80fd|\u4e0d\u5f97)\u4e0d", "\u5fc5\u987b", normalized)
    normalized = normalized.replace("\u4e0d\u4f46", "")

    predicates = (
        (
            "\u53bb",
            "\u524d\u5f80",
            "\u51fa\u5dee",
            "\u51fa\u884c",
            "\u542f\u7a0b",
        )
        if domain == "travel"
        else (
            "\u8054\u7cfb",
            "\u6c9f\u901a",
            "\u63a8\u8fdb",
            "\u63d0\u4ea4",
            "\u5f00\u5ead",
            "\u6536\u5230",
            "\u901a\u77e5",
            "\u53cd\u9988",
            "\u67e5\u63a7",
            "\u7acb\u6848",
            "\u5c65\u884c",
            "\u5b8c\u6210",
            "\u51c6\u5907",
            "\u5904\u7406",
            "\u7533\u8bf7",
            "\u9001\u8fbe",
            "\u8c03\u89e3",
            "\u548c\u89e3",
            "\u56de\u6b3e",
        )
    )
    predicate_pattern = "(?:" + "|".join(map(re.escape, predicates)) + ")"
    negative_patterns = (
        rf"(?:\u6ca1\u6709|\u8fd8\u6ca1\u6709|\u8fd8\u6ca1|\u5c1a\u672a|\u672a\u66fe){{1}}.{{0,6}}?{predicate_pattern}",
        rf"(?:\u672a){predicate_pattern}",
        rf"\u4e0d(?:\u518d|\u4f1a|\u6253\u7b97|\u51c6\u5907|\u60f3|\u80fd|\u9700\u8981|\u8ba1\u5212)?{predicate_pattern}",
        rf"(?:\u6ca1\u6cd5|\u65e0\u6cd5|\u672a\u80fd|\u6ca1\u80fd|\u4e0d\u65b9\u4fbf|\u4e0d\u4fbf).{{0,3}}?{predicate_pattern}",
        rf"{predicate_pattern}(?:\u4e0d\u4e86|\u4e0d\u6210|\u4e0d\u4e0a)",
    )
    if any(re.search(pattern, normalized) for pattern in negative_patterns):
        return "negative"
    if domain == "travel" and re.search(
        r"(?:\u53d6\u6d88|\u64a4\u9500).{0,8}(?:\u51fa\u5dee|\u884c\u7a0b|\u51fa\u884c)"
        r"|(?:\u51fa\u5dee|\u884c\u7a0b|\u51fa\u884c).{0,8}(?:\u53d6\u6d88|\u64a4\u9500|\u4e0d\u53bb\u4e86)",
        normalized,
    ):
        return "negative"
    if domain == "case" and re.search(
        r"(?:\u6682\u65e0|\u6ca1\u6709|\u5c1a\u65e0|\u65e0|\u6ca1)(?:\u4efb\u4f55|\u5176\u4ed6|\u65b0\u7684|\u65b0)?(?:\u8fdb\u5c55|\u6d88\u606f|\u53cd\u9988|\u53d8\u5316|\u95ee\u9898|\u98ce\u9669|\u5b89\u6392|\u7ed3\u679c)",
        normalized,
    ):
        return "negative"
    return "affirmative"


def _case_fact_contract_reason(
    entity: ConversationEntity | None,
    segment_text: str,
) -> str:
    if entity is None:
        return "case_fact_contract_incomplete"
    if (
        assess_case_progress_statement(segment_text).reason_code
        == "assistant_service_request"
    ):
        return "case_statement_not_asserted"
    attributes = entity.attributes
    if str(attributes.get("statement_mode") or "") != "asserted":
        return "case_statement_not_asserted"
    substantive_values: list[str] = []
    for name in (
        "stage",
        "case_stage",
        "case_node",
        "normalized_fact",
        "current_status",
        "hearing_readiness",
    ):
        value = str(attributes.get(name) or "").strip()
        if value:
            substantive_values.append(value)
    for name in (
        "factual_progress",
        "completed_actions",
        "next_actions",
        "blocking_issues",
    ):
        values = attributes.get(name)
        if isinstance(values, (list, tuple)):
            substantive_values.extend(
                str(value).strip() for value in values if str(value).strip()
            )
    evidence = _grounded_evidence_fragments(
        attributes.get("evidence_spans"),
        segment_text,
    )
    if not evidence:
        evidence = _bounded_case_evidence_tail_repair(
            attributes.get("evidence_spans"),
            segment_text,
            claim_anchors=tuple(substantive_values),
        )
    if not evidence:
        return "case_evidence_not_grounded"
    case_reference = _normalize_reference(entity.value)
    meaningful_evidence = tuple(
        fragment
        for fragment in evidence
        if _normalize_reference(fragment)
        and _normalize_reference(fragment) != case_reference
    )
    if not substantive_values or not meaningful_evidence:
        return "case_fact_contract_incomplete"
    assessment = evaluate_assertion_polarity_contract(
        domain="case",
        segment_text=segment_text,
        statement_mode=str(attributes.get("statement_mode") or ""),
        evidence_fragments=evidence,
        claim_anchors=tuple(substantive_values),
    )
    return assessment.reason_code


def _bounded_case_evidence_tail_repair(
    raw_spans: Any,
    segment_text: str,
    *,
    claim_anchors: tuple[str, ...],
) -> tuple[str, ...]:
    """Repair a small end-offset drift while preserving exact fact evidence.

    This validator cannot infer a domain, action, case, or fact.  The proposal
    must already contain an asserted case action; the span start must be valid,
    the tail overrun is capped, and an exact proposed fact anchor must occur in
    the repaired fragment from the same segment.
    """

    max_tail_overrun = 4
    if not isinstance(raw_spans, (list, tuple)) or not raw_spans:
        return ()
    normalized_anchors = tuple(
        _normalize_reference(anchor)
        for anchor in claim_anchors
        if str(anchor or "").strip()
        and _normalize_reference(anchor) in _normalize_reference(segment_text)
    )
    if not normalized_anchors:
        return ()
    repaired: list[str] = []
    for raw_span in raw_spans:
        if (
            not isinstance(raw_span, (list, tuple))
            or len(raw_span) != 2
            or isinstance(raw_span[0], bool)
            or isinstance(raw_span[1], bool)
            or not isinstance(raw_span[0], int)
            or not isinstance(raw_span[1], int)
        ):
            return ()
        start, end = raw_span
        if (
            start < 0
            or start >= len(segment_text)
            or end <= start
            or end <= len(segment_text)
            or end - len(segment_text) > max_tail_overrun
        ):
            return ()
        fragment = segment_text[start:]
        normalized_fragment = _normalize_reference(fragment)
        if not fragment.strip() or not any(
            anchor in normalized_fragment for anchor in normalized_anchors
        ):
            return ()
        repaired.append(fragment)
    return tuple(repaired)


def _travel_assertion_contract_reason(
    entity: ConversationEntity | None,
    segment_text: str,
    *,
    occurred_at: datetime,
    timezone_name: str,
) -> str:
    if entity is None:
        return "travel_assertion_contract_incomplete"
    attributes = entity.attributes
    if str(attributes.get("statement_mode") or "") != "asserted":
        return "travel_statement_not_asserted"
    if str(attributes.get("traveler_scope") or "") != "self":
        return "travel_not_current_user"
    evidence = _grounded_evidence_fragments(
        attributes.get("evidence_spans"),
        segment_text,
    )
    if not evidence:
        evidence = _grounded_travel_claim_fallback(
            entity,
            segment_text,
            occurred_at=occurred_at,
            timezone_name=timezone_name,
        )
        if not evidence:
            return "travel_evidence_not_grounded"
    assessment = evaluate_assertion_polarity_contract(
        domain="travel",
        segment_text=segment_text,
        statement_mode=str(attributes.get("statement_mode") or ""),
        evidence_fragments=evidence,
        claim_anchors=tuple(
            str(value).strip()
            for value in (
                attributes.get("destination"),
                attributes.get("purpose"),
                entity.value,
            )
            if str(value or "").strip()
        ),
    )
    return assessment.reason_code


def _grounded_travel_claim_fallback(
    entity: ConversationEntity,
    segment_text: str,
    *,
    occurred_at: datetime,
    timezone_name: str,
) -> tuple[str, ...]:
    attributes = entity.attributes
    entity_value = str(entity.value or "").strip()
    destination = str(attributes.get("destination") or "").strip()
    date_hint = str(attributes.get("date_hint") or "").strip()
    purpose = str(attributes.get("purpose") or "").strip()
    date_claim_grounded = _travel_date_claim_grounded(
        date_hint,
        entity_value,
        occurred_at=occurred_at,
        timezone_name=timezone_name,
    )
    # An exact but underspecified source date (for example, a week without a
    # day) is still evidence for an asserted trip.  It must not authorize a
    # write: the caller resolves the date next and creates InformationPending
    # when resolution is impossible.  This branch only prevents a valid
    # missing-information request from being mislabeled as ungrounded input.
    underspecified_date_grounded = bool(
        date_hint
        and _resolve_travel_date(
            date_hint,
            occurred_at=occurred_at,
            timezone_name=timezone_name,
        )
        is None
        and _normalize_reference(date_hint) in _normalize_reference(entity_value)
    )
    if (
        not entity_value
        or entity_value not in segment_text
        or not destination
        or _normalize_reference(destination) not in _normalize_reference(entity_value)
        or not purpose
        or _normalize_reference(purpose) not in _normalize_reference(entity_value)
        or not (date_claim_grounded or underspecified_date_grounded)
    ):
        return ()
    return (entity_value,)


def _travel_date_claim_grounded(
    date_hint: str,
    evidence_text: str,
    *,
    occurred_at: datetime,
    timezone_name: str,
) -> bool:
    resolved = _resolve_travel_date(
        date_hint,
        occurred_at=occurred_at,
        timezone_name=timezone_name,
    )
    if resolved is None:
        return False
    if _normalize_reference(date_hint) in _normalize_reference(evidence_text):
        return True
    for relative_hint in ("\u4eca\u5929", "\u660e\u5929", "\u540e\u5929"):
        if relative_hint not in evidence_text:
            continue
        if _resolve_travel_date(
            relative_hint,
            occurred_at=occurred_at,
            timezone_name=timezone_name,
        ) == resolved:
            return True
    return False


def _grounded_evidence_fragments(raw_spans: Any, segment_text: str) -> tuple[str, ...]:
    if not isinstance(raw_spans, (list, tuple)) or not raw_spans:
        return ()
    fragments: list[str] = []
    for raw_span in raw_spans:
        if (
            not isinstance(raw_span, (list, tuple))
            or len(raw_span) != 2
            or isinstance(raw_span[0], bool)
            or isinstance(raw_span[1], bool)
            or not isinstance(raw_span[0], int)
            or not isinstance(raw_span[1], int)
        ):
            return ()
        start, end = raw_span
        if start < 0 or end <= start or end > len(segment_text):
            return ()
        fragment = segment_text[start:end]
        if not fragment.strip():
            return ()
        fragments.append(fragment)
    return tuple(fragments)


def _normalize_reference(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _resolve_travel_date(
    date_hint: str,
    *,
    occurred_at: datetime,
    timezone_name: str,
) -> str | None:
    try:
        local_now = occurred_at.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        return None
    normalized = re.sub(r"[_\-]", "", _normalize_reference(date_hint))
    offsets = {
        "今天": 0,
        "today": 0,
        "明天": 1,
        "tomorrow": 1,
        "后天": 2,
        "dayaftertomorrow": 2,
    }
    if normalized in offsets:
        return (local_now.date() + timedelta(days=offsets[normalized])).isoformat()
    if normalized in {"下周一", "nextmonday"}:
        return (local_now.date() + timedelta(days=7 - local_now.weekday())).isoformat()
    try:
        return datetime.fromisoformat(date_hint).date().isoformat()
    except ValueError:
        return None


def _canonical_travel_response(segment_text: str) -> str:
    normalized = _normalize_reference(segment_text)
    aliases = {
        "accept": "accept",
        "\u9700\u8981": "accept",
        "\u540c\u610f": "accept",
        "\u53ef\u4ee5": "accept",
        "decline": "decline",
        "\u4e0d\u9700\u8981": "decline",
        "\u62d2\u7edd": "decline",
        "later": "later",
        "\u7a0d\u540e": "later",
        "\u7a0d\u540e\u786e\u8ba4": "later",
        "changed": "changed",
        "\u884c\u7a0b\u53d8\u4e86": "changed",
        "cancel": "cancel",
        "\u53d6\u6d88": "cancel",
        "\u53d6\u6d88\u51fa\u5dee": "cancel",
    }
    return aliases.get(normalized, "")


def _parse_aware_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _parse_in_timezone(value: Any, timezone_name: str) -> datetime | None:
    parsed = _parse_aware_datetime(value)
    if parsed is None:
        return None
    try:
        return parsed.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        return None


def _resolve_active_case_followup(
    *,
    turn: CognitiveTurn,
    case_id: str,
    pending_id: str,
) -> dict[str, Any] | None:
    """Resolve one trusted, live follow-up continuation without recency guesses."""

    if not pending_id:
        return None
    now = _aware(turn.occurred_at)
    active: list[dict[str, Any]] = []
    for raw in turn.resources.get("active_case_progress_followups") or ():
        if not isinstance(raw, Mapping):
            continue
        candidate = dict(raw)
        candidate_pending_id = str(
            candidate.get("pending_id")
            or candidate.get("notification_id")
            or ""
        )
        expires_at = _parse_aware_datetime(candidate.get("expires_at"))
        if (
            candidate_pending_id != pending_id
            or str(candidate.get("case_id") or "") != case_id
            or str(candidate.get("assigned_user_id") or "") != turn.user_id
            or str(candidate.get("conversation_id") or "")
            != turn.conversation_id
            or str(candidate.get("task_status") or "") != "waiting_for_reply"
            or str(candidate.get("message_status") or "")
            not in {"accepted_by_provider", "delivery_confirmed"}
            or not str(candidate.get("provider_message_id") or "").strip()
            or expires_at is None
            or expires_at <= now
        ):
            continue
        required = (
            "pending_version",
            "followup_id",
            "task_id",
            "task_version",
            "case_version",
            "expected_state_version",
        )
        if any(candidate.get(name) is None for name in required):
            continue
        candidate["pending_id"] = candidate_pending_id
        active.append(candidate)
    return active[0] if len(active) == 1 else None


def _resolve_followup_snoozed_until(
    value: str,
    *,
    now: datetime,
) -> datetime | None:
    compact = "".join(str(value or "").split()).casefold()
    if compact in {"tomorrow", "\u660e\u5929", "\u660e\u5929\u518d\u95ee"}:
        return (now + timedelta(days=1)).replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        )
    if compact in {
        "next_week",
        "\u4e0b\u5468",
        "\u4e0b\u5468\u518d\u95ee",
        "\u4e0b\u5468\u4e00",
    }:
        days = 7 - now.weekday()
        return (now + timedelta(days=days)).replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        )
    parsed = _parse_aware_datetime(value)
    if parsed is None or parsed <= now or parsed > now + timedelta(days=365):
        return None
    return parsed


def _stable_object_ref(
    *,
    trace_id: str,
    action: RequiredAction,
    domain: str,
    raw_object_ref: Mapping[str, Any],
) -> dict[str, Any]:
    explicit_object_type = str(raw_object_ref.get("object_type") or "")
    explicit_stable_id = str(raw_object_ref.get("stable_id") or "")
    if explicit_object_type and explicit_stable_id:
        return {
            "object_type": explicit_object_type,
            "stable_id": explicit_stable_id,
            "version": raw_object_ref.get("version"),
        }
    if domain == "report":
        object_type = str(raw_object_ref.get("object_type") or "")
        if object_type not in {"daily_report", "periodic_report"}:
            object_type = "daily_report"
        return {
            "object_type": object_type,
            "stable_id": str(raw_object_ref.get("report_id") or ""),
            "version": int(raw_object_ref.get("version") or 0),
        }
    if domain == "case":
        return {
            "object_type": "case",
            "stable_id": str(raw_object_ref.get("case_id") or ""),
            "version": int(raw_object_ref.get("version") or 0),
        }
    if domain == "travel":
        stable_id = str(raw_object_ref.get("stable_id") or "").strip()
        if stable_id:
            return {
                "object_type": "travel_intent",
                "stable_id": stable_id,
                "version": raw_object_ref.get("version"),
            }
        return {
            "object_type": "travel_intent",
            "stable_id": _stable_uuid("travel-intent", trace_id, action.action_id),
            "version": None,
        }
    return {
        "object_type": domain or "runtime",
        "stable_id": _stable_uuid("object", trace_id, action.action_id),
        "version": None,
    }


def _authorization_claims(
    *,
    action: RequiredAction,
    operation: str,
    entity_by_id: Mapping[str, ConversationEntity],
    segment: SemanticSegment,
    raw_object_ref: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...], str]:
    entity = _single_entity(action, entity_by_id)
    attributes = dict(entity.attributes) if entity is not None else {}
    embedded_authority = raw_object_ref.get("authority_scope")
    embedded_changed_fields = raw_object_ref.get("allowed_changed_fields")
    if isinstance(embedded_authority, Mapping) and isinstance(
        embedded_changed_fields, (list, tuple)
    ):
        authority_scope = dict(embedded_authority)
        allowed_changed_fields = tuple(
            str(field_name) for field_name in embedded_changed_fields
        )
    elif action.action_type == "capture_daily_event":
        authority_scope = {
            **dict(raw_object_ref),
            "field": str(attributes.get("field") or ""),
            "raw_fact": entity.value if entity is not None else segment.text,
            "segment_text": segment.text,
        }
        allowed_changed_fields = ("section", "items")
    elif action.action_type == "record_case_progress":
        authority_scope = {
            **dict(raw_object_ref),
            "raw_fact": segment.text,
            "case_reference": entity.value if entity is not None else "",
            "attributes": attributes,
        }
        allowed_changed_fields = (
            "summary",
            "details",
            "progress_type",
            "current_status",
            "next_actions",
            "hearing_readiness",
            "blocking_issues",
        )
    elif action.action_type == "record_travel_event":
        authority_scope = {
            **dict(raw_object_ref),
            "raw_fact": segment.text,
            "purpose": str(attributes.get("purpose") or ""),
        }
        allowed_changed_fields = (
            "destination",
            "start_at",
            "end_at",
            "purpose_summary",
        )
    else:
        authority_scope = {
            **dict(raw_object_ref),
            "raw_fact": segment.text,
        }
        allowed_changed_fields = ()
    claims = {
        "action_id": action.action_id,
        "operation": operation,
        "segment_text_sha256": segment.text_hash,
        "authority_scope": authority_scope,
        "allowed_changed_fields": list(allowed_changed_fields),
    }
    return authority_scope, allowed_changed_fields, _sha256_json(claims)


def _issue_ticket(
    *,
    turn: CognitiveTurn,
    scope: Mapping[str, str],
    trace_id: str,
    decision_id: str,
    source_turn_id: str,
    expected_conversation_state_version: int,
    action_id: str,
    segment_id: str,
    segment_text_sha256: str,
    segment_start_offset: int,
    segment_end_offset: int,
    domain: str,
    operation: str,
    object_ref: Mapping[str, Any],
    authority_scope: Mapping[str, Any],
    allowed_changed_fields: tuple[str, ...],
    fact_claims_sha256: str,
    ttl: timedelta,
) -> AdmissionTicket:
    issued_at = _aware(turn.occurred_at)
    authorized_command_sha256 = _sha256_json(
        {
            "domain": domain,
            "operation": operation,
            "object_ref": dict(object_ref),
            "authority_scope": dict(authority_scope),
            "allowed_changed_fields": list(allowed_changed_fields),
            "fact_claims_sha256": fact_claims_sha256,
        }
    )
    stable_parts = (
        trace_id,
        decision_id,
        action_id,
        segment_id,
        segment_text_sha256,
        domain,
        operation,
        _canonical_json(object_ref),
        fact_claims_sha256,
        authorized_command_sha256,
        ADMISSION_CONTRACT_VERSION,
        ADMISSION_POLICY_VERSION,
    )
    ticket_id = _stable_uuid("ticket", *stable_parts)
    return AdmissionTicket(
        ticket_id=ticket_id,
        tenant_id=scope["tenant_id"],
        user_id=scope["user_id"],
        conversation_id=turn.conversation_id,
        source_message_id=turn.message_id,
        action_id=action_id,
        segment_id=segment_id,
        domain=domain,
        operation=operation,
        object_ref=dict(object_ref),
        contract_version=ADMISSION_CONTRACT_VERSION,
        issued_at=issued_at,
        expires_at=issued_at + ttl,
        idempotency_key=_stable_digest("admission", *stable_parts),
        trace_id=trace_id,
        decision_id=decision_id,
        source_turn_id=source_turn_id,
        segment_text_sha256=segment_text_sha256,
        segment_start_offset=segment_start_offset,
        segment_end_offset=segment_end_offset,
        expected_conversation_state_version=expected_conversation_state_version,
        authority_scope=dict(authority_scope),
        allowed_changed_fields=allowed_changed_fields,
        fact_claims_sha256=fact_claims_sha256,
        authorized_command_sha256=authorized_command_sha256,
        policy_version=ADMISSION_POLICY_VERSION,
    )


def _create_information_pending(
    *,
    turn: CognitiveTurn,
    scope: Mapping[str, str],
    trace_id: str,
    decision_id: str,
    source_turn_id: str,
    expected_conversation_state_version: int,
    action: RequiredAction,
    entity_by_id: Mapping[str, ConversationEntity],
    segment_id: str,
    segment_text_sha256: str,
    segment_start_offset: int,
    segment_end_offset: int,
    domain: str,
    operation: str,
    object_ref: Mapping[str, Any] | None,
    created_at: datetime,
    ttl: timedelta,
) -> InformationPending:
    entity = _single_entity(action, entity_by_id)
    attributes = dict(entity.attributes) if entity is not None else {}
    if action.action_type == "record_travel_event":
        missing_fields = ("travel_date",)
        destination = str(attributes.get("destination") or "").strip()
        question_snapshot: dict[str, Any] = {
            "question_key": "travel_date_required",
            "destination": destination,
        }
        acceptable_answer_forms: dict[str, Any] = {
            "field": "travel_date",
            "value_types": ["relative_date", "iso_date"],
            "examples": ["明天", "2026-07-15"],
        }
    else:
        raise ValueError(
            f"information pending contract is not defined for {action.action_type}"
        )
    pending_id = _stable_uuid(
        "information-pending",
        trace_id,
        decision_id,
        action.action_id,
        segment_id,
        *missing_fields,
    )
    stable_parts = (
        trace_id,
        decision_id,
        pending_id,
        scope["tenant_id"],
        scope["user_id"],
        turn.conversation_id,
        action.action_id,
        segment_id,
        segment_text_sha256,
        domain,
        operation,
    )
    return InformationPending(
        pending_id=pending_id,
        trace_id=trace_id,
        decision_id=decision_id,
        tenant_id=scope["tenant_id"],
        user_id=scope["user_id"],
        conversation_id=turn.conversation_id,
        source_turn_id=source_turn_id,
        source_message_id=turn.message_id,
        segment_id=segment_id,
        segment_text_sha256=segment_text_sha256,
        segment_start_offset=segment_start_offset,
        segment_end_offset=segment_end_offset,
        domain=domain,
        operation=operation,
        object_ref=dict(object_ref) if object_ref is not None else None,
        expected_conversation_state_version=expected_conversation_state_version,
        missing_fields=missing_fields,
        question_snapshot=question_snapshot,
        acceptable_answer_forms=acceptable_answer_forms,
        created_at=created_at,
        expires_at=created_at + ttl,
        idempotency_key=_stable_digest("information-pending", *stable_parts),
    )


def _create_trusted_selection_request(
    *,
    turn: CognitiveTurn,
    scope: Mapping[str, str],
    trace_id: str,
    decision_id: str,
    source_turn_id: str,
    expected_conversation_state_version: int,
    action: RequiredAction,
    entity_by_id: Mapping[str, ConversationEntity],
    segment: SemanticSegment,
    candidate_records: Any,
    created_at: datetime,
    ttl: timedelta,
) -> TrustedSelectionRequest | None:
    """Create a non-writable Selection request from trusted visible cases."""

    if action.action_type != "record_case_progress" or not isinstance(
        candidate_records, (list, tuple)
    ):
        return None
    entity = _single_entity(action, entity_by_id)
    if entity is None or entity.entity_type != "case_ref":
        return None
    candidates: list[TrustedSelectionCandidateRef] = []
    seen: set[str] = set()
    for raw in candidate_records:
        if not isinstance(raw, Mapping):
            continue
        stable_id = str(raw.get("case_id") or "").strip()
        label = str(
            raw.get("case_name") or raw.get("case_number") or stable_id
        ).strip()
        try:
            version = int(raw.get("version"))
        except (TypeError, ValueError):
            continue
        if not stable_id or stable_id in seen or not label or version < 0:
            continue
        seen.add(stable_id)
        candidates.append(
            TrustedSelectionCandidateRef(
                stable_id=stable_id,
                version=version,
                label=label,
            )
        )
    if len(candidates) < 2:
        return None

    request_id = _stable_uuid(
        "trusted-selection-request",
        trace_id,
        decision_id,
        action.action_id,
        segment.segment_id,
        *(f"{item.stable_id}:{item.version}" for item in candidates),
    )
    sub_decision_id = _stable_uuid(
        "trusted-selection-sub-decision",
        decision_id,
        action.action_id,
    )
    command_id = _stable_uuid(
        "trusted-selection-command",
        request_id,
        sub_decision_id,
    )
    raw_command = {
        "command_id": command_id,
        "decision_id": decision_id,
        "sub_decision_id": sub_decision_id,
        "command_type": "record_case_progress_candidate",
        "target_system": "case_progress",
        "entity_ids": [entity.entity_id],
        "payload": {
            "entities": [
                {
                    "entity_id": entity.entity_id,
                    "entity_type": entity.entity_type,
                    "value": entity.value,
                    "confidence": entity.confidence,
                    "attributes": dict(entity.attributes),
                }
            ],
            "parameters": dict(action.parameters),
            "source_segments": [
                {
                    "segment_id": segment.segment_id,
                    "text": segment.text,
                    "text_hash": segment.text_hash,
                    "start_offset": segment.start_offset,
                    "end_offset": segment.end_offset,
                }
            ],
        },
        "execution_mode": "candidate",
        "idempotency_key": _stable_digest(
            "trusted-selection-command",
            scope["tenant_id"],
            scope["user_id"],
            turn.conversation_id,
            turn.message_id,
            action.action_id,
        ),
    }
    continuation_payload = protect_selection_continuation_payload(
        {
            "typed_business_command": raw_command,
            "bind": {
                "entity_type": "case_ref",
                "attribute": "case_id",
            },
        }
    )
    answer_forms: dict[str, str] = {}
    chinese_ordinals = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
    for index, candidate in enumerate(candidates, start=1):
        answer_forms[str(index)] = candidate.stable_id
        answer_forms[f"第{index}个"] = candidate.stable_id
        answer_forms[f"第{index}条"] = candidate.stable_id
        if index <= len(chinese_ordinals):
            ordinal = chinese_ordinals[index - 1]
            answer_forms[f"第{ordinal}个"] = candidate.stable_id
            answer_forms[f"第{ordinal}条"] = candidate.stable_id
        answer_forms[candidate.label] = candidate.stable_id
    return TrustedSelectionRequest(
        selection_request_id=request_id,
        trace_id=trace_id,
        decision_id=decision_id,
        tenant_id=scope["tenant_id"],
        user_id=scope["user_id"],
        conversation_id=turn.conversation_id,
        source_turn_id=source_turn_id,
        source_message_id=turn.message_id,
        action_id=action.action_id,
        segment_id=segment.segment_id,
        segment_text_sha256=segment.text_hash,
        segment_start_offset=segment.start_offset,
        segment_end_offset=segment.end_offset,
        domain="case",
        operation="record_case_progress",
        expected_conversation_state_version=expected_conversation_state_version,
        candidates=tuple(candidates),
        acceptable_answer_forms=answer_forms,
        continuation_payload=continuation_payload,
        created_at=created_at,
        expires_at=created_at + ttl,
        idempotency_key=_stable_digest(
            "trusted-selection-request",
            scope["tenant_id"],
            request_id,
        ),
    )


def _stable_digest(prefix: str, *parts: str) -> str:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()}"


def _stable_uuid(namespace: str, *parts: str) -> str:
    material = "\x1f".join((namespace, *(str(part) for part in parts)))
    return str(uuid5(NAMESPACE_URL, f"agent2-domain-admission:{material}"))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _semantic_proposal_sha256(proposal: SemanticInterpretation) -> str:
    return _sha256_json(
        {
            "intents": list(proposal.intents),
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
                for segment in proposal.segments
            ],
            "entities": [
                {
                    "entity_id": entity.entity_id,
                    "entity_type": entity.entity_type,
                    "value": entity.value,
                    "confidence": entity.confidence,
                    "attributes": dict(entity.attributes),
                }
                for entity in proposal.entities
            ],
            "confidence": proposal.confidence,
            "required_actions": [
                {
                    "action_id": action.action_id,
                    "action_type": action.action_type,
                    "intent": action.intent,
                    "entity_ids": list(action.entity_ids),
                    "parameters": dict(action.parameters),
                }
                for action in proposal.required_actions
            ],
        }
    )


def _admission_summary(decisions: tuple[AdmissionDecision, ...]) -> str:
    statuses = {decision.status for decision in decisions}
    if not statuses:
        return "no_op"
    if statuses == {"admitted"}:
        return "admitted"
    if "admitted" in statuses:
        return "partially_admitted"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "blocked"
