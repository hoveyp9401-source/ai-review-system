from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import re

from app.agent2.business.admission import (
    trusted_case_ticket_hint,
    validate_business_candidate_admission,
    validate_case_followup_ticket_object,
    validate_case_ticket_object,
    validate_travel_collaboration_ticket_object,
    validate_travel_ticket_object,
)
from app.agent2.business.case_progress import CaseRecord, resolve_case_target
from app.agent2.business.contracts import (
    BusinessCommand,
    BusinessCommandContext,
    CreateCaseProgress,
    SnoozeCaseFollowup,
    CreateTravelIntent,
    ListAssignedCases,
    QueryOperationStatus,
    RespondTravelCollaboration,
)
from app.agent2.business.travel import LocationRegistry, resolve_travel_window
from app.agent2.case_statement_contract import is_nonassertive_case_progress
from app.agent2.command_planner_v3 import (
    PlanningBlock,
    TypedBusinessCommand,
    is_self_scoped_case_inventory_hint,
)
from app.agent2.case_followup_commands import TriggerCaseFollowupNow, UpdateCaseFollowupPolicy


@dataclass(frozen=True)
class CaseFollowupPolicyRecord:
    case_id: str
    assigned_user_id: str
    version: int


@dataclass(frozen=True)
class Phase2CommandCompilation:
    command: BusinessCommand | None = None
    block: PlanningBlock | None = None
    outcome_context: dict = field(default_factory=dict)


class Phase2BusinessCommandCompiler:
    """Canonicalize semantic candidates into executable domain commands.

    This is the deterministic policy seam: it consumes structured semantic
    candidates, never raw text at the executor, and fails closed when a target
    or required parameter is not uniquely resolvable.
    """

    def __init__(self, *, locations: LocationRegistry | None = None):
        self.locations = locations or LocationRegistry.default()

    def compile(
        self,
        candidate: TypedBusinessCommand,
        context: BusinessCommandContext,
        *,
        cases: tuple[CaseRecord, ...],
        followup_policies: tuple[CaseFollowupPolicyRecord, ...] = (),
    ) -> Phase2CommandCompilation:
        admission_block = validate_business_candidate_admission(candidate, context)
        if admission_block is not None:
            return Phase2CommandCompilation(block=admission_block)
        entities = tuple(candidate.payload.get("entities") or ())
        if len(entities) != 1 or not isinstance(entities[0], dict):
            return self._blocked(candidate, "single_semantic_entity_required")
        entity = entities[0]
        if candidate.command_type == "list_assigned_cases":
            return self._compile_case_inventory(candidate, entity)
        if candidate.command_type == "query_operation_status":
            return self._compile_operation_status(candidate, entity)
        if candidate.command_type == "record_travel_candidate":
            return self._compile_travel(candidate, entity, context)
        if candidate.command_type == "record_case_progress_candidate":
            return self._compile_case_progress(candidate, entity, context, cases)
        if candidate.command_type == "respond_travel_collaboration_candidate":
            return self._compile_travel_response(candidate, entity)
        if candidate.command_type == "update_case_followup_policy_candidate":
            return self._compile_followup_policy(
                candidate, entity, context, cases, followup_policies
            )
        if candidate.command_type == "trigger_case_followup_now_candidate":
            return self._compile_followup_now(
                candidate, entity, context, cases, followup_policies
            )
        return self._blocked(candidate, "unsupported_phase2_business_candidate")

    def _compile_case_inventory(
        self,
        candidate: TypedBusinessCommand,
        entity: dict,
    ) -> Phase2CommandCompilation:
        if entity.get("entity_type") != "case_query":
            return self._blocked(candidate, "case_query_entity_required")
        attributes = (
            entity.get("attributes")
            if isinstance(entity.get("attributes"), dict)
            else {}
        )
        matter_hint = str(attributes.get("matter_hint") or "").strip()
        if matter_hint and not is_self_scoped_case_inventory_hint(matter_hint):
            return self._blocked(candidate, "case_inventory_must_not_bind_party")
        return Phase2CommandCompilation(
            command=ListAssignedCases(command_id=str(candidate.command_id))
        )

    def _compile_operation_status(
        self,
        candidate: TypedBusinessCommand,
        entity: dict,
    ) -> Phase2CommandCompilation:
        if entity.get("entity_type") != "operation_status_query":
            return self._blocked(candidate, "operation_status_query_entity_required")
        attributes = (
            entity.get("attributes")
            if isinstance(entity.get("attributes"), dict)
            else {}
        )
        domain = str(attributes.get("domain") or "").strip()
        if domain not in {"case_progress", "travel"}:
            return self._blocked(candidate, "operation_status_domain_unsupported")
        return Phase2CommandCompilation(
            command=QueryOperationStatus(
                command_id=str(candidate.command_id),
                domain=domain,
            )
        )

    def _compile_travel(
        self,
        candidate: TypedBusinessCommand,
        entity: dict,
        context: BusinessCommandContext,
    ) -> Phase2CommandCompilation:
        if entity.get("entity_type") != "travel_event":
            return self._blocked(candidate, "travel_event_entity_required")
        attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        destination_raw = str(attributes.get("destination") or entity.get("value") or "").strip()
        purpose = str(attributes.get("purpose") or "").strip()
        location = self.locations.resolve(destination_raw)
        if location.status != "resolved":
            return Phase2CommandCompilation(
                block=self._blocked(
                    candidate, "travel_location_needs_clarification"
                ).block,
                outcome_context={
                    "destination": destination_raw,
                    "purpose": purpose,
                },
            )
        date_hint = str(attributes.get("date_hint") or "").strip()
        date_text = f"{_localized_date_hint(date_hint)} {entity.get('value') or ''}"
        try:
            window = resolve_travel_window(date_text, reference_date=context.occurred_at.date())
        except (ValueError, OverflowError):
            return Phase2CommandCompilation(
                block=self._blocked(
                    candidate, "travel_time_needs_clarification"
                ).block,
                outcome_context={
                    "destination": location.destination_normalized,
                    "purpose": purpose,
                },
            )
        confidence = float(entity.get("confidence") or 0)
        if confidence < 0.85:
            return self._blocked(candidate, "travel_confidence_too_low")
        timezone = context.occurred_at.tzinfo
        admission_block = validate_travel_ticket_object(
            candidate,
            destination=destination_raw,
            travel_date=window.start_date.isoformat(),
            purpose=purpose,
        )
        if admission_block is not None:
            return Phase2CommandCompilation(block=admission_block)
        start_at = datetime.combine(window.start_date, time.min, tzinfo=timezone)
        end_at = datetime.combine(window.end_date, time.max, tzinfo=timezone)
        return Phase2CommandCompilation(
            command=CreateTravelIntent(
                command_id=str(candidate.command_id),
                destination_raw=destination_raw,
                destination_normalized=location.destination_normalized,
                city_code=location.city_code,
                province_code=location.province_code,
                start_at=start_at,
                end_at=end_at,
                time_precision=window.precision,
                purpose_summary=purpose,
                related_case_ids=(),
                confidence=confidence,
            ),
            outcome_context={
                "destination": location.destination_normalized,
                "date_label": _travel_date_label(window.start_date, window.end_date),
                "purpose": str(attributes.get("purpose") or "").strip(),
            },
        )

    def _compile_case_progress(
        self,
        candidate: TypedBusinessCommand,
        entity: dict,
        context: BusinessCommandContext,
        cases: tuple[CaseRecord, ...],
    ) -> Phase2CommandCompilation:
        if entity.get("entity_type") != "case_ref":
            return self._blocked(candidate, "case_ref_entity_required")
        target = str(entity.get("value") or "").strip()
        source_text = _source_segment_text(candidate)
        if not source_text:
            return self._blocked(candidate, "case_progress_source_segment_required")
        ticket_operation = str(candidate.admission_ticket.get("operation") or "")
        ticket_hint = trusted_case_ticket_hint(
            candidate,
            context,
            source_text=source_text,
        )
        if (
            candidate.admission_ticket
            and not candidate.admission_required
            and ticket_operation == "record_case_progress"
        ):
            if ticket_hint is None:
                return self._blocked(candidate, "admission_ticket_claims_mismatch")
            ticket_case_id, ticket_case_version = ticket_hint
            case = next(
                (
                    item
                    for item in cases
                    if item.case_id == ticket_case_id
                    and item.version == ticket_case_version
                ),
                None,
            )
            if case is None:
                reason_code = (
                    "admission_ticket_object_version_conflict"
                    if any(item.case_id == ticket_case_id for item in cases)
                    else "admission_ticket_object_mismatch"
                )
                return self._blocked(candidate, reason_code)
        else:
            if (
                candidate.admission_ticket
                and not candidate.admission_required
                and ticket_operation != "snooze_case_followup"
            ):
                return self._blocked(candidate, "admission_ticket_claims_mismatch")
            resolution = resolve_case_target(
                target,
                cases,
                context,
                source_text=source_text,
            )
            if resolution.status == "needs_clarification":
                candidates_by_id = {item.case_id: item for item in cases}
                return self._blocked(
                    candidate,
                    "case_target_needs_clarification",
                    ",".join(resolution.candidate_case_ids),
                    metadata={
                        "selection": {
                            "domain": "case_progress",
                            "operation": "create",
                            "candidates": [
                                {
                                    "stable_id": case_id,
                                    "version": candidates_by_id[case_id].version,
                                    "label": candidates_by_id[case_id].case_name,
                                }
                                for case_id in resolution.candidate_case_ids
                                if case_id in candidates_by_id
                            ],
                            "continuation_payload": {
                                "typed_business_command": candidate.as_dict(),
                                "bind": {
                                    "entity_type": "case_ref",
                                    "attribute": "case_id",
                                },
                            },
                        }
                    },
                )
            if resolution.status != "resolved":
                return Phase2CommandCompilation(
                    block=self._blocked(candidate, "case_target_not_found").block,
                    outcome_context={"case_name": target},
                )
            case = next(
                (item for item in cases if item.case_id == resolution.case_id),
                None,
            )
            if case is None:
                return Phase2CommandCompilation(
                    block=self._blocked(candidate, "case_target_not_found").block,
                    outcome_context={"case_name": target},
                )
        admission_block = validate_case_ticket_object(
            candidate,
            case_id=case.case_id,
            case_version=case.version,
        )
        if admission_block is not None:
            return Phase2CommandCompilation(block=admission_block)
        attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        requested_snooze = str(attributes.get("requested_snooze") or "").strip()
        if requested_snooze:
            pending_id = str(
                attributes.get("followup_notification_id") or ""
            ).strip()
            if candidate.admission_required:
                authority_scope = candidate.admission_ticket.get(
                    "authority_scope"
                )
                if not isinstance(authority_scope, dict):
                    return self._blocked(
                        candidate,
                        "admission_ticket_claims_mismatch",
                    )
                snoozed_until = _parse_ticket_snoozed_until(
                    authority_scope.get("snoozed_until")
                )
                if (
                    str(authority_scope.get("requested_snooze") or "")
                    != requested_snooze
                    or str(authority_scope.get("pending_id") or "")
                    != pending_id
                    or str(authority_scope.get("case_id") or "")
                    != case.case_id
                ):
                    return self._blocked(
                        candidate,
                        "admission_ticket_claims_mismatch",
                    )
            else:
                snoozed_until = _resolve_snoozed_until(
                    requested_snooze,
                    now=context.occurred_at,
                )
            if not pending_id:
                return self._blocked(candidate, "case_followup_pending_required")
            if snoozed_until is None:
                return self._blocked(candidate, "case_followup_snooze_time_invalid")
            return Phase2CommandCompilation(
                command=SnoozeCaseFollowup(
                    command_id=str(candidate.command_id), pending_id=pending_id,
                    case_id=case.case_id, snoozed_until=snoozed_until,
                ),
                outcome_context={
                    "case_name": case.case_name,
                    "snoozed_until": snoozed_until.isoformat(),
                },
            )
        if _case_progress_is_non_assertive(source_text):
            return self._blocked(candidate, "case_progress_not_asserted")
        confidence = float(entity.get("confidence") or 0)
        if confidence < 0.85:
            return self._blocked(candidate, "case_progress_confidence_too_low")
        return Phase2CommandCompilation(
            command=CreateCaseProgress(
                command_id=str(candidate.command_id),
                case_id=case.case_id,
                occurred_at=context.occurred_at,
                progress_type=str(attributes.get("stage") or "general_update"),
                summary=source_text,
                details="",
                related_party_ids=(),
                related_document_ids=(),
                related_travel_intent_ids=(),
                confidence=confidence,
                followup_notification_id=str(
                    attributes.get("followup_notification_id") or ""
                ).strip(),
                lifecycle_stage=_grounded_text(
                    attributes.get("case_stage"), source_text
                ),
                lifecycle_node=_grounded_text(
                    attributes.get("case_node"), source_text
                ),
                current_status=_grounded_text(
                    attributes.get("current_status"), source_text
                ),
                next_actions=_grounded_strings(
                    attributes.get("next_actions"), source_text
                ),
                hearing_readiness=_grounded_text(
                    attributes.get("hearing_readiness"), source_text
                ),
                blocking_issues=_grounded_strings(
                    attributes.get("blocking_issues"), source_text
                ),
            ),
            outcome_context={
                "case_name": case.case_name,
                "content": source_text,
                "case_fact_extraction": {
                    "raw_text": source_text,
                    "normalized_fact": source_text,
                    "factual_progress": list(_grounded_strings(
                        attributes.get("factual_progress"), source_text
                    )),
                    "completed_actions": list(_grounded_strings(
                        attributes.get("completed_actions"), source_text
                    )),
                    "current_status": _grounded_text(
                        attributes.get("current_status"), source_text
                    ),
                    "next_actions": list(_grounded_strings(
                        attributes.get("next_actions"), source_text
                    )),
                    "action_time_scope": str(
                        attributes.get("action_time_scope") or "unknown"
                    ),
                    "hearing_readiness": _grounded_text(
                        attributes.get("hearing_readiness"), source_text
                    ),
                    "blocking_issues": list(_grounded_strings(
                        attributes.get("blocking_issues"), source_text
                    )),
                    "requested_snooze": str(
                        attributes.get("requested_snooze") or ""
                    ),
                    "report_preference": str(
                        attributes.get("report_preference") or "automatic"
                    ),
                    "confidence": confidence,
                    "evidence_spans": list(
                        attributes.get("evidence_spans") or []
                    ),
                },
            },
        )

    def _compile_travel_response(
        self,
        candidate: TypedBusinessCommand,
        entity: dict,
    ) -> Phase2CommandCompilation:
        if entity.get("entity_type") != "travel_collaboration_ref":
            return self._blocked(candidate, "travel_collaboration_ref_required")
        attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        candidate_id = str(attributes.get("candidate_id") or "").strip()
        response = str(attributes.get("response") or "").strip().casefold()
        normalized_response = {
            "accept": "accept",
            "需要": "accept",
            "同意": "accept",
            "decline": "decline",
            "不需要": "decline",
            "拒绝": "decline",
            "later": "later",
            "稍后": "later",
            "稍后确认": "later",
            "changed": "changed",
            "行程变了": "changed",
            "cancel": "cancel",
            "取消": "cancel",
            "取消出差": "cancel",
        }.get(response)
        if not candidate_id:
            return self._blocked(candidate, "travel_collaboration_target_required")
        if normalized_response is None:
            return self._blocked(candidate, "travel_collaboration_response_required")
        admission_block = validate_travel_collaboration_ticket_object(
            candidate,
            candidate_id=candidate_id,
            response=normalized_response,
        )
        if admission_block is not None:
            return Phase2CommandCompilation(block=admission_block)
        object_ref = candidate.admission_ticket.get("object_ref")
        expected_version = (
            int(object_ref.get("version"))
            if candidate.admission_required and isinstance(object_ref, dict)
            else None
        )
        return Phase2CommandCompilation(
            command=RespondTravelCollaboration(
                command_id=str(candidate.command_id),
                candidate_id=candidate_id,
                response=normalized_response,  # type: ignore[arg-type]
                expected_version=expected_version,
            )
        )

    def _compile_followup_policy(
        self,
        candidate: TypedBusinessCommand,
        entity: dict,
        context: BusinessCommandContext,
        cases: tuple[CaseRecord, ...],
        policies: tuple[CaseFollowupPolicyRecord, ...],
    ) -> Phase2CommandCompilation:
        if entity.get("entity_type") != "case_followup_policy":
            return self._blocked(candidate, "case_followup_policy_entity_required")
        attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        case_hint = str(attributes.get("case_hint") or entity.get("value") or "").strip()
        resolution = resolve_case_target(case_hint, cases, context)
        if resolution.status == "needs_clarification":
            candidates_by_id = {item.case_id: item for item in cases}
            return self._blocked(
                candidate, "case_target_needs_clarification",
                metadata={
                    "selection": {
                        "domain": "case_followup_policy", "operation": "update",
                        "candidates": [
                            {"stable_id": case_id, "version": candidates_by_id[case_id].version,
                             "label": candidates_by_id[case_id].case_name}
                            for case_id in resolution.candidate_case_ids
                            if case_id in candidates_by_id
                        ],
                        "continuation_payload": {"typed_business_command": candidate.as_dict()},
                    }
                },
            )
        if resolution.status != "resolved":
            return self._blocked(candidate, "case_target_not_found")
        case = next((item for item in cases if item.case_id == resolution.case_id), None)
        if case is None or case.case_id not in context.allowed_case_ids:
            return self._blocked(candidate, "case_access_denied")
        current = next(
            (
                item for item in policies
                if item.case_id == case.case_id
                and item.assigned_user_id == context.actor_user_id
            ),
            None,
        )
        current_version = current.version if current is not None else 0
        admission_block = validate_case_followup_ticket_object(
            candidate,
            case_id=case.case_id,
            assigned_user_id=context.actor_user_id,
            policy_version=current_version,
        )
        if admission_block is not None:
            return Phase2CommandCompilation(block=admission_block)
        cadence = str(attributes.get("cadence_type") or "").strip()
        if cadence not in {
            "daily", "weekly", "every_15_days", "monthly", "custom_interval",
            "event_only", "manual_only", "paused", "disabled",
        }:
            return self._blocked(candidate, "followup_cadence_invalid")
        custom_days = attributes.get("custom_interval_days")
        if cadence == "custom_interval" and (
            not isinstance(custom_days, int) or isinstance(custom_days, bool) or custom_days <= 0
        ):
            return self._blocked(candidate, "custom_interval_required")
        return Phase2CommandCompilation(
            command=UpdateCaseFollowupPolicy(
                command_id=str(candidate.command_id), tenant_id=context.tenant_id,
                case_id=case.case_id, assigned_user_id=context.actor_user_id,
                expected_version=current_version,
                policy_source="case_manual_override", cadence_type=cadence,
                enabled=bool(attributes.get("enabled", cadence not in {"disabled", "paused"})),
                force_manual_override=False, source_turn_id=context.source_message_id,
                idempotency_key=candidate.idempotency_key,
                custom_interval_days=custom_days if isinstance(custom_days, int) else None,
                snoozed_until=None,
                hearing_reminders_enabled=attributes.get("hearing_reminders_enabled"),
                stage_transition_enabled=attributes.get("stage_transition_enabled"),
                node_transition_enabled=attributes.get("node_transition_enabled"),
            ),
            outcome_context={
                "case_name": case.case_name, "cadence_type": cadence,
                "expected_version": current_version,
            },
        )

    def _compile_followup_now(
        self,
        candidate: TypedBusinessCommand,
        entity: dict,
        context: BusinessCommandContext,
        cases: tuple[CaseRecord, ...],
        policies: tuple[CaseFollowupPolicyRecord, ...],
    ) -> Phase2CommandCompilation:
        if entity.get("entity_type") != "case_followup_policy":
            return self._blocked(candidate, "case_followup_policy_entity_required")
        attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        case_hint = str(attributes.get("case_hint") or entity.get("value") or "").strip()
        resolution = resolve_case_target(case_hint, cases, context)
        if resolution.status == "needs_clarification":
            candidates_by_id = {item.case_id: item for item in cases}
            return self._blocked(
                candidate, "case_target_needs_clarification",
                metadata={
                    "selection": {
                        "domain": "case_followup_policy", "operation": "trigger_now",
                        "candidates": [
                            {"stable_id": case_id, "version": candidates_by_id[case_id].version,
                             "label": candidates_by_id[case_id].case_name}
                            for case_id in resolution.candidate_case_ids
                            if case_id in candidates_by_id
                        ],
                        "continuation_payload": {"typed_business_command": candidate.as_dict()},
                    }
                },
            )
        if resolution.status != "resolved":
            return self._blocked(candidate, "case_target_not_found")
        case = next((item for item in cases if item.case_id == resolution.case_id), None)
        if case is None or case.case_id not in context.allowed_case_ids:
            return self._blocked(candidate, "case_access_denied")
        current = next(
            (
                item for item in policies
                if item.case_id == case.case_id
                and item.assigned_user_id == context.actor_user_id
            ),
            None,
        )
        current_version = current.version if current is not None else 0
        admission_block = validate_case_followup_ticket_object(
            candidate,
            case_id=case.case_id,
            assigned_user_id=context.actor_user_id,
            policy_version=current_version,
        )
        if admission_block is not None:
            return Phase2CommandCompilation(block=admission_block)
        return Phase2CommandCompilation(
            command=TriggerCaseFollowupNow(
                command_id=str(candidate.command_id), tenant_id=context.tenant_id,
                case_id=case.case_id, assigned_user_id=context.actor_user_id,
                source_turn_id=context.source_message_id,
                idempotency_key=candidate.idempotency_key,
                expected_policy_version=current_version,
            ),
            outcome_context={"case_name": case.case_name},
        )

    @staticmethod
    def _blocked(
        candidate: TypedBusinessCommand,
        reason_code: str,
        detail: str = "",
        metadata: dict | None = None,
    ) -> Phase2CommandCompilation:
        return Phase2CommandCompilation(
            block=PlanningBlock(
                action_id=str(candidate.sub_decision_id),
                reason_code=reason_code,
                detail=detail,
                metadata=dict(metadata or {}),
            )
        )


def _source_segment_text(candidate: TypedBusinessCommand) -> str:
    segments = candidate.payload.get("source_segments") or ()
    if not isinstance(segments, (list, tuple)) or len(segments) != 1 or not isinstance(segments[0], dict):
        return ""
    return str(segments[0].get("text") or "").strip()


def _resolve_snoozed_until(value: str, *, now: datetime) -> datetime | None:
    compact = "".join(str(value or "").split()).casefold()
    if compact in {"tomorrow", "明天", "明天再问"}:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    if compact in {"next_week", "下周", "下周再问", "下周一"}:
        days = 7 - now.weekday()
        return (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
    try:
        parsed = datetime.fromisoformat(compact.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(compact)
        except ValueError:
            return None
        parsed = datetime.combine(parsed_date, time(9), tzinfo=now.tzinfo)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    if parsed <= now or parsed > now + timedelta(days=365):
        return None
    return parsed


def _parse_ticket_snoozed_until(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _localized_date_hint(value: str) -> str:
    return {
        "tomorrow": "明天",
        "day_after_tomorrow": "后天",
        "next_monday": "下周一",
    }.get(value.casefold(), value)


def _travel_date_label(start_date, end_date) -> str:
    if start_date == end_date:
        return f"{start_date.month}月{start_date.day}日"
    return f"{start_date.month}月{start_date.day}日—{end_date.month}月{end_date.day}日"


def _grounded_text(value: object, raw_text: str) -> str:
    text = str(value or "").strip()
    return text if text and text in raw_text else ""


def _grounded_strings(values: object, raw_text: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(
        text for value in values
        if (text := str(value or "").strip()) and text in raw_text
    )


def _case_progress_is_non_assertive(value: str) -> bool:
    return is_nonassertive_case_progress(value)
