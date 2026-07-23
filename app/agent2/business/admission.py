from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping

from app.agent2.admission_contracts import ADMISSION_CONTRACT_VERSION
from app.agent2.case_followup_commands import (
    TriggerCaseFollowupNow,
    UpdateCaseFollowupPolicy,
)
from app.agent2.business.contracts import (
    BusinessCommand,
    BusinessCommandContext,
    BusinessCommandError,
    CreateCaseProgress,
    CreateTravelIntent,
    DeleteCaseProgress,
    LinkCaseProgress,
    RespondTravelCollaboration,
    SnoozeCaseFollowup,
    UpdateCaseProgress,
    UpdateTravelIntent,
)
from app.agent2.command_planner_v3 import PlanningBlock, TypedBusinessCommand


_CANDIDATE_CONTRACTS: dict[str, tuple[str, str]] = {
    "record_case_progress_candidate": ("case", "record_case_progress"),
    "record_travel_candidate": ("travel", "record_travel_event"),
    "update_travel_candidate": ("travel", "update_travel_event"),
    "update_case_progress_candidate": ("case", "update_case_progress"),
    "delete_case_progress_candidate": ("case", "delete_case_progress"),
    "link_case_progress_candidate": ("case", "link_case_progress"),
    "respond_travel_collaboration_candidate": (
        "travel",
        "respond_travel_collaboration",
    ),
    "update_case_followup_policy_candidate": (
        "case",
        "update_case_followup_policy",
    ),
    "trigger_case_followup_now_candidate": (
        "case",
        "trigger_case_followup_now",
    ),
}


def bind_business_execution_context(
    candidate: TypedBusinessCommand,
    context: BusinessCommandContext,
) -> BusinessCommandContext:
    if not candidate.admission_required:
        return context
    return replace(
        context,
        execution_started_at=context.execution_started_at or datetime.now(timezone.utc),
        admission_ticket=dict(candidate.admission_ticket),
        admission_required=True,
        admission_action_id=candidate.admission_action_id,
        admission_operation=candidate.admission_operation,
    )


def validate_business_candidate_admission(
    candidate: TypedBusinessCommand,
    context: BusinessCommandContext,
) -> PlanningBlock | None:
    """Validate the trusted scope and lifetime before domain compilation."""

    if not candidate.admission_required:
        return None
    expected = _CANDIDATE_CONTRACTS.get(candidate.command_type)
    if expected is None:
        return _block(candidate, "unknown_admission_contract")
    ticket = candidate.admission_ticket
    if not isinstance(ticket, dict) or not ticket:
        return _block(candidate, "missing_admission_ticket")
    if (
        candidate.command_type == "record_case_progress_candidate"
        and str(ticket.get("operation") or "") == "snooze_case_followup"
    ):
        expected = ("case", "snooze_case_followup")
    domain, operation = expected
    if str(ticket.get("contract_version") or "") != ADMISSION_CONTRACT_VERSION:
        return _block(candidate, "unknown_admission_contract")
    if (
        str(ticket.get("ticket_status") or "issued") != "issued"
        or ticket.get("executor_revalidation_required", True) is not True
        or ticket.get("proves_business_write", False) is not False
    ):
        return _block(candidate, "admission_ticket_inactive")
    if any(
        str(ticket.get(name) or "") != expected_value
        for name, expected_value in (
            ("tenant_id", context.tenant_id),
            ("user_id", context.actor_user_id),
            ("conversation_id", context.conversation_id),
            ("source_message_id", context.source_message_id),
        )
    ):
        return _block(candidate, "admission_ticket_scope_mismatch")
    if (
        str(ticket.get("action_id") or "") != candidate.admission_action_id
        or str(ticket.get("operation") or "") != operation
        or candidate.admission_operation != operation
        or str(ticket.get("domain") or "") != domain
    ):
        return _block(candidate, "admission_ticket_operation_mismatch")
    ticket_authority = ticket.get("authority_scope")
    if (
        isinstance(ticket_authority, Mapping)
        and str(ticket_authority.get("selection_pending_id") or "")
    ):
        selection_evidence = candidate.payload.get("selection_evidence")
        if not isinstance(selection_evidence, Mapping):
            return _block(candidate, "admission_ticket_segment_mismatch")
        source_segment = selection_evidence
        source_text = str(source_segment.get("text") or "")
        source_text_hash = str(source_segment.get("text_sha256") or "")
        if (
            str(source_segment.get("source_message_id") or "")
            != context.source_message_id
            or str(source_segment.get("segment_id") or "")
            != str(ticket.get("segment_id") or "")
        ):
            return _block(candidate, "admission_ticket_segment_mismatch")
    else:
        segment_ids = {
            str(item.get("segment_id") or "")
            for item in candidate.payload.get("source_segments") or ()
            if isinstance(item, dict)
        }
        if str(ticket.get("segment_id") or "") not in segment_ids:
            return _block(candidate, "admission_ticket_segment_mismatch")
        matching_segments = [
            item
            for item in candidate.payload.get("source_segments") or ()
            if isinstance(item, dict)
            and str(item.get("segment_id") or "") == str(ticket.get("segment_id") or "")
        ]
        if len(matching_segments) != 1:
            return _block(candidate, "admission_ticket_segment_mismatch")
        source_segment = matching_segments[0]
        source_text = str(source_segment.get("text") or "")
        source_text_hash = str(source_segment.get("text_hash") or "")
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if (
        source_hash != source_text_hash
        or source_hash != str(ticket.get("segment_text_sha256") or "")
        or int(source_segment.get("start_offset", -1))
        != int(ticket.get("segment_start_offset", -2))
        or int(source_segment.get("end_offset", -1))
        != int(ticket.get("segment_end_offset", -2))
    ):
        return _block(candidate, "admission_ticket_segment_mismatch")
    if context.conversation_state_version is None:
        return _block(candidate, "admission_state_version_required")
    try:
        expected_state_version = int(
            ticket.get("expected_conversation_state_version")
        )
    except (TypeError, ValueError):
        return _block(candidate, "admission_ticket_state_version_conflict")
    if expected_state_version != context.conversation_state_version:
        return _block(candidate, "admission_ticket_state_version_conflict")
    authority_scope = ticket.get("authority_scope")
    allowed_changed_fields = ticket.get("allowed_changed_fields")
    if not isinstance(authority_scope, Mapping) or not isinstance(
        allowed_changed_fields, list
    ):
        return _block(candidate, "admission_ticket_claims_mismatch")
    fact_claims_sha256 = _sha256_json(
        {
            "action_id": candidate.admission_action_id,
            "operation": candidate.admission_operation,
            "segment_text_sha256": source_hash,
            "authority_scope": dict(authority_scope),
            "allowed_changed_fields": allowed_changed_fields,
        }
    )
    if fact_claims_sha256 != str(ticket.get("fact_claims_sha256") or ""):
        return _block(candidate, "admission_ticket_claims_mismatch")
    authorized_command_sha256 = _sha256_json(
        {
            "domain": domain,
            "operation": operation,
            "object_ref": ticket.get("object_ref"),
            "authority_scope": dict(authority_scope),
            "allowed_changed_fields": allowed_changed_fields,
            "fact_claims_sha256": fact_claims_sha256,
        }
    )
    if authorized_command_sha256 != str(
        ticket.get("authorized_command_sha256") or ""
    ):
        return _block(candidate, "admission_ticket_claims_mismatch")
    if context.execution_started_at is None:
        return _block(candidate, "admission_execution_time_required")
    issued_at = _parse_aware_datetime(ticket.get("issued_at"))
    expires_at = _parse_aware_datetime(ticket.get("expires_at"))
    if issued_at is None or expires_at is None or issued_at >= expires_at:
        return _block(candidate, "admission_ticket_time_invalid")
    if context.execution_started_at < issued_at or context.execution_started_at >= expires_at:
        return _block(candidate, "admission_ticket_expired")
    return None


def trusted_case_ticket_hint(
    candidate: TypedBusinessCommand,
    context: BusinessCommandContext,
    *,
    source_text: str,
) -> tuple[str, int] | None:
    """Return the deterministic Case target from a valid Admission artifact.

    Shadow Admission still issues a fully hashed, non-writing ticket.  The
    compiler may use that artifact to avoid resolving the model's normalized
    alias a second time.  This does not authorize execution: enforced mode
    still performs the complete Ticket validation and the executor always
    revalidates live tenant/write permission.
    """

    if candidate.command_type != "record_case_progress_candidate":
        return None
    ticket = candidate.admission_ticket
    if not isinstance(ticket, Mapping) or not ticket:
        return None
    if any(
        str(ticket.get(name) or "") != expected
        for name, expected in (
            ("contract_version", ADMISSION_CONTRACT_VERSION),
            ("tenant_id", context.tenant_id),
            ("user_id", context.actor_user_id),
            ("conversation_id", context.conversation_id),
            ("source_message_id", context.source_message_id),
            ("domain", "case"),
            ("operation", "record_case_progress"),
        )
    ):
        return None
    if (
        str(ticket.get("ticket_status") or "") != "issued"
        or ticket.get("executor_revalidation_required") is not True
        or ticket.get("proves_business_write") is not False
        or not str(ticket.get("ticket_id") or "").strip()
        or not str(ticket.get("action_id") or "").strip()
    ):
        return None
    object_ref = ticket.get("object_ref")
    authority_scope = ticket.get("authority_scope")
    allowed_changed_fields = ticket.get("allowed_changed_fields")
    entities = candidate.payload.get("entities") or ()
    source_segments = candidate.payload.get("source_segments") or ()
    if (
        not isinstance(object_ref, Mapping)
        or not isinstance(authority_scope, Mapping)
        or not isinstance(allowed_changed_fields, list)
        or len(entities) != 1
        or not isinstance(entities[0], Mapping)
        or len(source_segments) != 1
        or not isinstance(source_segments[0], Mapping)
    ):
        return None
    if set(allowed_changed_fields) != {
        "summary",
        "details",
        "progress_type",
        "current_status",
        "next_actions",
        "hearing_readiness",
        "blocking_issues",
    }:
        return None
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if (
        str(ticket.get("segment_id") or "")
        != str(source_segments[0].get("segment_id") or "")
        or not str(ticket.get("segment_id") or "").strip()
        or str(source_segments[0].get("text") or "") != source_text
        or str(source_segments[0].get("text_hash") or "") != source_hash
        or str(ticket.get("segment_text_sha256") or "") != source_hash
        or str(authority_scope.get("raw_fact") or "") != source_text
        or str(authority_scope.get("case_reference") or "")
        != str(entities[0].get("value") or "")
        or dict(authority_scope.get("attributes") or {})
        != dict(entities[0].get("attributes") or {})
    ):
        return None
    case_id = str(object_ref.get("stable_id") or "").strip()
    if (
        str(object_ref.get("object_type") or "") != "case"
        or not case_id
        or str(authority_scope.get("case_id") or "") != case_id
    ):
        return None
    try:
        case_version = int(object_ref.get("version"))
        authority_version = int(authority_scope.get("version"))
    except (TypeError, ValueError):
        return None
    if case_version < 0 or authority_version != case_version:
        return None
    fact_claims_sha256 = _sha256_json(
        {
            "action_id": str(ticket.get("action_id") or ""),
            "operation": "record_case_progress",
            "segment_text_sha256": source_hash,
            "authority_scope": dict(authority_scope),
            "allowed_changed_fields": allowed_changed_fields,
        }
    )
    if fact_claims_sha256 != str(ticket.get("fact_claims_sha256") or ""):
        return None
    authorized_command_sha256 = _sha256_json(
        {
            "domain": "case",
            "operation": "record_case_progress",
            "object_ref": dict(object_ref),
            "authority_scope": dict(authority_scope),
            "allowed_changed_fields": allowed_changed_fields,
            "fact_claims_sha256": fact_claims_sha256,
        }
    )
    if authorized_command_sha256 != str(
        ticket.get("authorized_command_sha256") or ""
    ):
        return None
    if context.conversation_state_version is not None:
        try:
            expected_state_version = int(
                ticket.get("expected_conversation_state_version")
            )
        except (TypeError, ValueError):
            return None
        if expected_state_version != context.conversation_state_version:
            return None
    issued_at = _parse_aware_datetime(ticket.get("issued_at"))
    expires_at = _parse_aware_datetime(ticket.get("expires_at"))
    execution_started_at = context.execution_started_at or datetime.now(timezone.utc)
    if (
        issued_at is None
        or expires_at is None
        or issued_at >= expires_at
        or execution_started_at < issued_at
        or execution_started_at >= expires_at
    ):
        return None
    return case_id, case_version


def validate_case_ticket_object(
    candidate: TypedBusinessCommand,
    *,
    case_id: str,
    case_version: int,
) -> PlanningBlock | None:
    if not candidate.admission_required:
        return None
    object_ref = candidate.admission_ticket.get("object_ref")
    authority_scope = candidate.admission_ticket.get("authority_scope")
    if candidate.admission_operation == "snooze_case_followup":
        entities = candidate.payload.get("entities") or ()
        attributes = (
            entities[0].get("attributes")
            if len(entities) == 1 and isinstance(entities[0], Mapping)
            else None
        )
        if not isinstance(object_ref, Mapping) or not isinstance(
            authority_scope, Mapping
        ) or not isinstance(attributes, Mapping):
            return _block(candidate, "admission_ticket_object_mismatch")
        try:
            pending_version = int(object_ref.get("version"))
            expected_pending_version = int(authority_scope.get("pending_version"))
            expected_case_version = int(authority_scope.get("case_version"))
        except (TypeError, ValueError):
            return _block(candidate, "admission_ticket_object_mismatch")
        if (
            str(object_ref.get("object_type") or "")
            != "case_followup_pending"
            or str(object_ref.get("stable_id") or "")
            != str(authority_scope.get("pending_id") or "")
            or str(authority_scope.get("pending_id") or "")
            != str(attributes.get("followup_notification_id") or "")
            or str(authority_scope.get("case_id") or "") != case_id
            or pending_version != expected_pending_version
            or expected_case_version != case_version
            or str(authority_scope.get("requested_snooze") or "")
            != str(attributes.get("requested_snooze") or "")
        ):
            return _block(candidate, "admission_ticket_object_mismatch")
        return None
    if not isinstance(object_ref, Mapping):
        return _block(candidate, "admission_ticket_object_mismatch")
    if (
        str(object_ref.get("object_type") or "") != "case"
        or str(object_ref.get("stable_id") or "") != case_id
    ):
        return _block(candidate, "admission_ticket_object_mismatch")
    try:
        expected_version = int(object_ref.get("version"))
    except (TypeError, ValueError):
        return _block(candidate, "admission_ticket_object_mismatch")
    if expected_version != case_version:
        return _block(candidate, "admission_ticket_object_version_conflict")
    authority_scope = candidate.admission_ticket.get("authority_scope")
    entities = candidate.payload.get("entities") or ()
    source_segments = candidate.payload.get("source_segments") or ()
    if (
        not isinstance(authority_scope, Mapping)
        or len(entities) != 1
        or not isinstance(entities[0], dict)
        or len(source_segments) != 1
        or not isinstance(source_segments[0], dict)
        or str(authority_scope.get("raw_fact") or "")
        != str(source_segments[0].get("text") or "")
        or str(authority_scope.get("case_reference") or "")
        != str(entities[0].get("value") or "")
        or dict(authority_scope.get("attributes") or {})
        != dict(entities[0].get("attributes") or {})
    ):
        return _block(candidate, "admission_ticket_claims_mismatch")
    return None


def validate_travel_ticket_object(
    candidate: TypedBusinessCommand,
    *,
    destination: str,
    travel_date: str,
    purpose: str,
) -> PlanningBlock | None:
    if not candidate.admission_required:
        return None
    object_ref = candidate.admission_ticket.get("object_ref")
    authority_scope = candidate.admission_ticket.get("authority_scope")
    if not isinstance(object_ref, Mapping) or not isinstance(authority_scope, Mapping):
        return _block(candidate, "admission_ticket_object_mismatch")
    if str(object_ref.get("object_type") or "") != "travel_intent":
        return _block(candidate, "admission_ticket_object_mismatch")
    if _normalize(authority_scope.get("destination")) != _normalize(destination):
        return _block(candidate, "admission_ticket_object_mismatch")
    if str(authority_scope.get("travel_date") or "") != travel_date:
        return _block(candidate, "admission_ticket_object_version_conflict")
    source_segments = candidate.payload.get("source_segments") or ()
    source_text = (
        str(source_segments[0].get("text") or "")
        if len(source_segments) == 1 and isinstance(source_segments[0], dict)
        else ""
    )
    if (
        str(authority_scope.get("raw_fact") or "") != source_text
        or str(authority_scope.get("purpose") or "").strip() != purpose.strip()
    ):
        return _block(candidate, "admission_ticket_claims_mismatch")
    return None


def validate_case_progress_ticket_object(
    candidate: TypedBusinessCommand,
    *,
    progress_id: str,
    case_id: str,
    progress_version: int,
) -> PlanningBlock | None:
    if not candidate.admission_required:
        return None
    object_ref = candidate.admission_ticket.get("object_ref")
    authority_scope = candidate.admission_ticket.get("authority_scope")
    if not isinstance(object_ref, Mapping) or not isinstance(authority_scope, Mapping):
        return _block(candidate, "admission_ticket_object_mismatch")
    if (
        str(object_ref.get("object_type") or "") != "case_progress"
        or str(object_ref.get("stable_id") or "") != progress_id
        or str(authority_scope.get("progress_id") or "") != progress_id
        or str(authority_scope.get("case_id") or "") != case_id
    ):
        return _block(candidate, "admission_ticket_object_mismatch")
    try:
        object_version = int(object_ref.get("version"))
        authority_version = int(authority_scope.get("version"))
    except (TypeError, ValueError):
        return _block(candidate, "admission_ticket_object_mismatch")
    if object_version != progress_version or authority_version != progress_version:
        return _block(candidate, "admission_ticket_object_version_conflict")
    entities = candidate.payload.get("entities") or ()
    if len(entities) != 1 or not isinstance(entities[0], Mapping):
        return _block(candidate, "admission_ticket_claims_mismatch")
    attributes = entities[0].get("attributes")
    if not isinstance(attributes, Mapping):
        return _block(candidate, "admission_ticket_claims_mismatch")
    expected_by_operation: dict[str, tuple[tuple[str, str], ...]] = {
        "update_case_progress": (
            ("replacement_summary", "summary"),
            ("replacement_details", "details"),
        ),
        "delete_case_progress": (("delete_reason", "delete_reason"),),
        "link_case_progress": (
            ("related_party_ids", "related_party_ids"),
            ("related_document_ids", "related_document_ids"),
            ("related_travel_intent_ids", "related_travel_intent_ids"),
        ),
    }
    operation = candidate.admission_operation
    allowed = tuple(candidate.admission_ticket.get("allowed_changed_fields") or ())
    for attribute_name, changed_field in expected_by_operation.get(operation, ()):
        if attribute_name not in authority_scope:
            if attributes.get(attribute_name) not in (None, "", [], ()):
                return _block(candidate, "admission_ticket_claims_mismatch")
            continue
        raw_value = attributes.get(attribute_name)
        authority_value = authority_scope.get(attribute_name)
        if isinstance(authority_value, (list, tuple)):
            if tuple(raw_value or ()) != tuple(authority_value):
                return _block(candidate, "admission_ticket_claims_mismatch")
        elif str(raw_value or "").strip() != str(authority_value or "").strip():
            return _block(candidate, "admission_ticket_claims_mismatch")
        if changed_field not in allowed:
            return _block(candidate, "admission_ticket_claims_mismatch")
    return None


def validate_travel_collaboration_ticket_object(
    candidate: TypedBusinessCommand,
    *,
    candidate_id: str,
    response: str,
) -> PlanningBlock | None:
    if not candidate.admission_required:
        return None
    object_ref = candidate.admission_ticket.get("object_ref")
    authority_scope = candidate.admission_ticket.get("authority_scope")
    if not isinstance(object_ref, Mapping) or not isinstance(authority_scope, Mapping):
        return _block(candidate, "admission_ticket_object_mismatch")
    if (
        str(object_ref.get("object_type") or "")
        != "travel_collaboration_candidate"
        or str(object_ref.get("stable_id") or "") != candidate_id
        or str(authority_scope.get("candidate_id") or "") != candidate_id
    ):
        return _block(candidate, "admission_ticket_object_mismatch")
    try:
        version = int(object_ref.get("version"))
        authority_version = int(authority_scope.get("version"))
    except (TypeError, ValueError):
        return _block(candidate, "admission_ticket_object_mismatch")
    if version <= 0 or authority_version != version:
        return _block(candidate, "admission_ticket_object_version_conflict")
    entities = candidate.payload.get("entities") or ()
    attributes = (
        entities[0].get("attributes")
        if len(entities) == 1 and isinstance(entities[0], Mapping)
        else None
    )
    if not isinstance(attributes, Mapping):
        return _block(candidate, "admission_ticket_claims_mismatch")
    semantic_response = str(attributes.get("response") or "").strip().casefold()
    if (
        str(attributes.get("candidate_id") or "") != candidate_id
        or str(authority_scope.get("response") or "") != response
        or semantic_response not in {response, _travel_response_surface(response)}
    ):
        return _block(candidate, "admission_ticket_claims_mismatch")
    return None


def validate_case_followup_ticket_object(
    candidate: TypedBusinessCommand,
    *,
    case_id: str,
    assigned_user_id: str,
    policy_version: int,
) -> PlanningBlock | None:
    if not candidate.admission_required:
        return None
    object_ref = candidate.admission_ticket.get("object_ref")
    authority_scope = candidate.admission_ticket.get("authority_scope")
    if not isinstance(object_ref, Mapping) or not isinstance(authority_scope, Mapping):
        return _block(candidate, "admission_ticket_object_mismatch")
    if (
        str(object_ref.get("object_type") or "") != "case_followup_policy"
        or str(object_ref.get("stable_id") or "") != case_id
        or str(authority_scope.get("case_id") or "") != case_id
        or str(authority_scope.get("assigned_user_id") or "") != assigned_user_id
    ):
        return _block(candidate, "admission_ticket_object_mismatch")
    try:
        object_version = int(object_ref.get("version"))
        authority_version = int(authority_scope.get("policy_version"))
    except (TypeError, ValueError):
        return _block(candidate, "admission_ticket_object_mismatch")
    if object_version != policy_version or authority_version != policy_version:
        return _block(candidate, "admission_ticket_object_version_conflict")
    entities = candidate.payload.get("entities") or ()
    attributes = (
        entities[0].get("attributes")
        if len(entities) == 1 and isinstance(entities[0], Mapping)
        else None
    )
    if not isinstance(attributes, Mapping):
        return _block(candidate, "admission_ticket_claims_mismatch")
    for field_name in (
        "cadence_type",
        "custom_interval_days",
        "snoozed_until",
        "enabled",
        "hearing_reminders_enabled",
        "stage_transition_enabled",
        "node_transition_enabled",
    ):
        if field_name in authority_scope:
            if attributes.get(field_name) != authority_scope.get(field_name):
                return _block(candidate, "admission_ticket_claims_mismatch")
        elif attributes.get(field_name) is not None:
            return _block(candidate, "admission_ticket_claims_mismatch")
    return None


def require_business_execution_admission(
    command: BusinessCommand,
    context: BusinessCommandContext,
) -> None:
    """Revalidate an enforced ticket immediately before any domain effect."""

    if not context.admission_required:
        return
    ticket = context.admission_ticket
    if not isinstance(ticket, dict) or not ticket:
        raise BusinessCommandError(
            "missing_admission_ticket", "admission", "admission ticket is required"
        )
    expected = _compiled_contract(command)
    if expected is None:
        raise BusinessCommandError(
            "unknown_admission_contract", "admission", "command has no admission contract"
        )
    domain, operation = expected
    if str(ticket.get("contract_version") or "") != ADMISSION_CONTRACT_VERSION:
        raise BusinessCommandError(
            "unknown_admission_contract", "admission", "unknown admission contract"
        )
    if (
        str(ticket.get("ticket_status") or "issued") != "issued"
        or ticket.get("executor_revalidation_required", True) is not True
        or ticket.get("proves_business_write", False) is not False
    ):
        raise BusinessCommandError(
            "admission_ticket_inactive", "admission", "ticket is not executable"
        )
    if any(
        str(ticket.get(name) or "") != expected_value
        for name, expected_value in (
            ("tenant_id", context.tenant_id),
            ("user_id", context.actor_user_id),
            ("conversation_id", context.conversation_id),
            ("source_message_id", context.source_message_id),
        )
    ):
        raise BusinessCommandError(
            "admission_ticket_scope_mismatch", "admission", "ticket scope changed"
        )
    if (
        str(ticket.get("action_id") or "") != context.admission_action_id
        or str(ticket.get("domain") or "") != domain
        or str(ticket.get("operation") or "") != operation
        or context.admission_operation != operation
    ):
        raise BusinessCommandError(
            "admission_ticket_operation_mismatch",
            "admission",
            "ticket operation changed",
        )
    if context.execution_started_at is None:
        raise BusinessCommandError(
            "admission_execution_time_required",
            "admission",
            "execution time is required",
        )
    if context.conversation_state_version is None:
        raise BusinessCommandError(
            "admission_state_version_required",
            "admission",
            "conversation state version is required",
        )
    try:
        expected_state_version = int(
            ticket.get("expected_conversation_state_version")
        )
    except (TypeError, ValueError) as exc:
        raise BusinessCommandError(
            "admission_ticket_state_version_conflict",
            "admission",
            "ticket state version is invalid",
        ) from exc
    if expected_state_version != context.conversation_state_version:
        raise BusinessCommandError(
            "admission_ticket_state_version_conflict",
            "admission",
            "conversation state changed",
        )
    issued_at = _parse_aware_datetime(ticket.get("issued_at"))
    expires_at = _parse_aware_datetime(ticket.get("expires_at"))
    if issued_at is None or expires_at is None or issued_at >= expires_at:
        raise BusinessCommandError(
            "admission_ticket_time_invalid", "admission", "ticket lifetime is invalid"
        )
    if context.execution_started_at < issued_at or context.execution_started_at >= expires_at:
        raise BusinessCommandError(
            "admission_ticket_expired", "admission", "ticket is not active"
        )
    _require_compiled_object_match(command, ticket)


def _compiled_contract(command: BusinessCommand) -> tuple[str, str] | None:
    if isinstance(command, CreateCaseProgress):
        return "case", "record_case_progress"
    if isinstance(command, SnoozeCaseFollowup):
        return "case", "snooze_case_followup"
    if isinstance(command, CreateTravelIntent):
        return "travel", "record_travel_event"
    if isinstance(command, UpdateTravelIntent):
        return "travel", "update_travel_event"
    if isinstance(command, UpdateCaseProgress):
        return "case", "update_case_progress"
    if isinstance(command, DeleteCaseProgress):
        return "case", "delete_case_progress"
    if isinstance(command, LinkCaseProgress):
        return "case", "link_case_progress"
    if isinstance(command, RespondTravelCollaboration):
        return "travel", "respond_travel_collaboration"
    if isinstance(command, UpdateCaseFollowupPolicy):
        return "case", "update_case_followup_policy"
    if isinstance(command, TriggerCaseFollowupNow):
        return "case", "trigger_case_followup_now"
    return None


def _require_compiled_object_match(
    command: BusinessCommand,
    ticket: Mapping[str, Any],
) -> None:
    object_ref = ticket.get("object_ref")
    if not isinstance(object_ref, Mapping):
        raise BusinessCommandError(
            "admission_ticket_object_mismatch", "admission", "ticket object is missing"
        )
    if isinstance(command, SnoozeCaseFollowup):
        authority_scope = ticket.get("authority_scope")
        object_matches = (
            str(object_ref.get("object_type") or "")
            == "case_followup_pending"
            and str(object_ref.get("stable_id") or "") == command.pending_id
            and isinstance(authority_scope, Mapping)
            and str(authority_scope.get("case_id") or "") == command.case_id
        )
        claims_match = (
            isinstance(authority_scope, Mapping)
            and str(authority_scope.get("pending_id") or "")
            == command.pending_id
            and str(authority_scope.get("snoozed_until") or "")
            == command.snoozed_until.isoformat()
        )
    elif isinstance(command, CreateCaseProgress):
        object_matches = (
            str(object_ref.get("object_type") or "") == "case"
            and str(object_ref.get("stable_id") or "") == command.case_id
        )
        claims_match = True
        if isinstance(command, CreateCaseProgress):
            authority_scope = ticket.get("authority_scope")
            claims_match = (
                isinstance(authority_scope, Mapping)
                and str(authority_scope.get("raw_fact") or "").strip()
                == command.summary.strip()
                and str(
                    (authority_scope.get("attributes") or {}).get("stage") or ""
                    if isinstance(authority_scope.get("attributes"), Mapping)
                    else ""
                ).strip()
                in {"", command.progress_type.strip()}
            )
    elif isinstance(command, CreateTravelIntent):
        authority_scope = ticket.get("authority_scope")
        object_matches = str(object_ref.get("object_type") or "") == "travel_intent"
        claims_match = (
            isinstance(authority_scope, Mapping)
            and _normalize(authority_scope.get("destination"))
            == _normalize(command.destination_raw)
            and str(authority_scope.get("travel_date") or "")
            == command.start_at.date().isoformat()
            and str(authority_scope.get("purpose") or "").strip()
            == command.purpose_summary.strip()
        )
    elif isinstance(command, UpdateTravelIntent):
        authority_scope = ticket.get("authority_scope")
        changed_fields = tuple(
            field_name
            for field_name, value in (
                ("start_at", command.start_at),
                ("end_at", command.end_at),
                ("destination_normalized", command.destination_normalized),
                ("city_code", command.city_code),
                ("status", command.status),
            )
            if value is not None
        )
        object_matches = (
            str(object_ref.get("object_type") or "") == "travel_intent"
            and str(object_ref.get("stable_id") or "") == command.travel_intent_id
            and _object_version(object_ref.get("version")) == command.expected_version
        )
        claims_match = (
            isinstance(authority_scope, Mapping)
            and str(authority_scope.get("travel_intent_id") or "")
            == command.travel_intent_id
            and _object_version(authority_scope.get("version"))
            == command.expected_version
            and str(authority_scope.get("status") or "") == str(command.status or "")
            and (
                command.start_at is None
                or str(authority_scope.get("start_at") or "")
                == command.start_at.isoformat()
            )
            and (
                command.end_at is None
                or str(authority_scope.get("end_at") or "")
                == command.end_at.isoformat()
            )
            and (
                command.destination_normalized is None
                or str(authority_scope.get("destination_normalized") or "")
                == command.destination_normalized
            )
            and (
                command.city_code is None
                or str(authority_scope.get("city_code") or "") == command.city_code
            )
            and tuple(ticket.get("allowed_changed_fields") or ()) == changed_fields
        )
    elif isinstance(command, (UpdateCaseProgress, DeleteCaseProgress, LinkCaseProgress)):
        authority_scope = ticket.get("authority_scope")
        object_matches = (
            str(object_ref.get("object_type") or "") == "case_progress"
            and str(object_ref.get("stable_id") or "") == command.progress_id
            and _object_version(object_ref.get("version")) == command.expected_version
        )
        claims_match = isinstance(authority_scope, Mapping)
        if isinstance(command, UpdateCaseProgress):
            claims_match = claims_match and (
                (command.summary is None and "replacement_summary" not in authority_scope)
                or command.summary == str(authority_scope.get("replacement_summary") or "")
            ) and (
                (command.details is None and "replacement_details" not in authority_scope)
                or command.details == str(authority_scope.get("replacement_details") or "")
            )
        elif isinstance(command, DeleteCaseProgress):
            claims_match = claims_match and command.reason == str(
                authority_scope.get("delete_reason") or ""
            )
        else:
            claims_match = claims_match and all(
                tuple(getattr(command, field_name))
                == tuple(authority_scope.get(field_name) or ())
                for field_name in (
                    "related_party_ids",
                    "related_document_ids",
                    "related_travel_intent_ids",
                )
            )
    elif isinstance(command, RespondTravelCollaboration):
        authority_scope = ticket.get("authority_scope")
        object_matches = (
            str(object_ref.get("object_type") or "")
            == "travel_collaboration_candidate"
            and str(object_ref.get("stable_id") or "") == command.candidate_id
            and command.expected_version is not None
            and _object_version(object_ref.get("version")) == command.expected_version
        )
        claims_match = (
            isinstance(authority_scope, Mapping)
            and str(authority_scope.get("response") or "") == command.response
            and str(authority_scope.get("candidate_id") or "")
            == command.candidate_id
        )
    elif isinstance(command, UpdateCaseFollowupPolicy):
        authority_scope = ticket.get("authority_scope")
        object_matches = (
            str(object_ref.get("object_type") or "") == "case_followup_policy"
            and str(object_ref.get("stable_id") or "") == command.case_id
            and _object_version(object_ref.get("version")) == command.expected_version
        )
        claims_match = isinstance(authority_scope, Mapping) and all(
            authority_scope.get(field_name) == getattr(command, field_name)
            for field_name in (
                "cadence_type",
                "custom_interval_days",
                "enabled",
                "hearing_reminders_enabled",
                "stage_transition_enabled",
                "node_transition_enabled",
            )
            if field_name in authority_scope
        )
    elif isinstance(command, TriggerCaseFollowupNow):
        authority_scope = ticket.get("authority_scope")
        object_matches = (
            str(object_ref.get("object_type") or "") == "case_followup_policy"
            and str(object_ref.get("stable_id") or "") == command.case_id
            and command.expected_policy_version is not None
            and _object_version(object_ref.get("version"))
            == command.expected_policy_version
        )
        claims_match = (
            isinstance(authority_scope, Mapping)
            and str(authority_scope.get("case_id") or "") == command.case_id
            and str(authority_scope.get("assigned_user_id") or "")
            == command.assigned_user_id
        )
    else:
        object_matches = False
        claims_match = False
    if not object_matches:
        raise BusinessCommandError(
            "admission_ticket_object_mismatch", "admission", "ticket object changed"
        )
    if not claims_match:
        raise BusinessCommandError(
            "admission_ticket_claims_mismatch", "admission", "authorized command changed"
        )


def _parse_aware_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _object_version(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        version = int(value)
    except (TypeError, ValueError):
        return None
    return version if version >= 0 else None


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-—_·•,，.。()（）\[\]【】]", "", text)


def _travel_response_surface(response: str) -> str:
    return {
        "accept": "\u9700\u8981",
        "decline": "\u4e0d\u9700\u8981",
        "later": "\u7a0d\u540e\u786e\u8ba4",
        "changed": "\u884c\u7a0b\u53d8\u4e86",
        "cancel": "\u53d6\u6d88\u51fa\u5dee",
    }.get(response, "")


def _sha256_json(value: Any) -> str:
    material = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _block(candidate: TypedBusinessCommand, reason_code: str) -> PlanningBlock:
    return PlanningBlock(str(candidate.sub_decision_id), reason_code)
