from __future__ import annotations

from typing import Any

from app.agent2.cognitive_core_v3 import SemanticInterpretation


ENTITY_ATTRIBUTE_KEYS = {
    "daily_event": frozenset({"field", "statement_mode", "context_reference"}),
    "daily_item_target": frozenset(
        {
            "target_item_ids",
            "replacement",
            "source_field",
            "target_field",
            "context_reference",
        }
    ),
    "case_query": frozenset({"matter_hint", "question", "context_reference"}),
    "operation_status_query": frozenset({"domain"}),
    "travel_event": frozenset(
        {
            "destination",
            "date_hint",
            "purpose",
            "statement_mode",
            "traveler_scope",
            "evidence_spans",
            "context_reference",
        }
    ),
    "travel_intent_ref": frozenset(
        {
            "travel_intent_id",
            "expected_version",
            "destination",
            "new_date_hint",
            "new_status",
            "context_reference",
        }
    ),
    "travel_collaboration_ref": frozenset({"candidate_id", "response", "context_reference"}),
    "daily_report": frozenset(
        {"report_id", "version", "report_date", "field", "items", "context_reference"}
    ),
    "case_ref": frozenset(
        {
            "stage", "case_stage", "case_node", "followup_notification_id", "normalized_fact",
            "factual_progress", "completed_actions", "current_status",
            "next_actions", "action_time_scope", "hearing_readiness",
            "blocking_issues", "requested_snooze", "report_preference",
            "evidence_spans", "statement_mode", "context_reference",
        }
    ),
    "case_progress_ref": frozenset(
        {
            "case_hint",
            "progress_id",
            "expected_version",
            "replacement_summary",
            "replacement_details",
            "delete_reason",
            "start_at",
            "end_at",
            "related_party_ids",
            "related_document_ids",
            "related_travel_intent_ids",
            "context_reference",
        }
    ),
    "case_followup_policy": frozenset(
        {
            "case_hint", "cadence_type", "custom_interval_days", "snoozed_until",
            "enabled", "hearing_reminders_enabled", "stage_transition_enabled",
            "node_transition_enabled", "evidence_spans", "context_reference",
        }
    ),
    "knowledge_query": frozenset({"query", "topic", "context_reference"}),
    "report_event": frozenset({"report_type", "field", "context_reference"}),
    "periodic_report": frozenset(
        {"report_type", "report_id", "version", "period_key", "context_reference"}
    ),
    "report_item_target": frozenset(
        {"report_type", "target_item_ids", "replacement", "context_reference"}
    ),
}
ACTION_PARAMETER_KEYS = {
    "continue_pending": frozenset({"pending_id", "bound_action"}),
    "capture_daily_event": frozenset({"confirmed_pending_id"}),
    "edit_daily_item": frozenset({"confirmed_pending_id"}),
    "delete_daily_item": frozenset({"confirmed_pending_id"}),
    "merge_daily_items": frozenset({"confirmed_pending_id"}),
    "replace_daily_section": frozenset({"confirmed_pending_id"}),
    "move_daily_items": frozenset({"confirmed_pending_id"}),
    "query_daily_report": frozenset({"confirmed_pending_id"}),
    "copy_previous_daily_report": frozenset({"confirmed_pending_id"}),
    "clear_daily_section": frozenset({"confirmed_pending_id"}),
    "clear_daily_report": frozenset({"confirmed_pending_id"}),
    "reopen_daily_report": frozenset({"confirmed_pending_id"}),
    "copy_current_work_to_tomorrow": frozenset({"confirmed_pending_id"}),
    "complete_previous_daily_plan": frozenset({"confirmed_pending_id"}),
    "record_case_progress": frozenset({"confirmed_pending_id"}),
    "update_case_progress": frozenset({"confirmed_pending_id"}),
    "delete_case_progress": frozenset({"confirmed_pending_id"}),
    "query_case_progress": frozenset({"confirmed_pending_id"}),
    "query_operation_status": frozenset({"confirmed_pending_id"}),
    "link_case_progress": frozenset({"confirmed_pending_id"}),
    "submit_daily_report": frozenset({"confirmed_pending_id"}),
    "answer_case_query": frozenset({"confirmed_pending_id"}),
    "record_travel_event": frozenset({"confirmed_pending_id"}),
    "update_travel_event": frozenset({"confirmed_pending_id"}),
    "respond_travel_collaboration": frozenset({"confirmed_pending_id"}),
    "search_enterprise_knowledge": frozenset({"confirmed_pending_id"}),
    "capture_report_event": frozenset({"confirmed_pending_id"}),
    "query_periodic_report": frozenset({"confirmed_pending_id"}),
    "submit_periodic_report": frozenset({"confirmed_pending_id"}),
    "edit_periodic_report_item": frozenset({"confirmed_pending_id"}),
    "delete_periodic_report_item": frozenset({"confirmed_pending_id"}),
    "update_case_followup_policy": frozenset({"confirmed_pending_id"}),
    "trigger_case_followup_now": frozenset({"confirmed_pending_id"}),
}
CONTEXT_REFERENCE_KEYS = frozenset(
    {"intent", "context_id", "selection", "value_source"}
)
STRING_ENTITY_ATTRIBUTES = {
    "daily_event": frozenset({"field", "statement_mode"}),
    "daily_item_target": frozenset({"replacement", "source_field", "target_field"}),
    "case_query": frozenset({"matter_hint", "question"}),
    "operation_status_query": frozenset({"domain"}),
    "travel_event": frozenset(
        {"destination", "date_hint", "purpose", "statement_mode", "traveler_scope"}
    ),
    "travel_intent_ref": frozenset(
        {"travel_intent_id", "destination", "new_date_hint", "new_status"}
    ),
    "travel_collaboration_ref": frozenset({"candidate_id", "response"}),
    "daily_report": frozenset({"report_id", "report_date", "field"}),
    "case_ref": frozenset({
        "stage", "case_stage", "case_node", "followup_notification_id", "normalized_fact", "current_status",
        "action_time_scope", "hearing_readiness", "requested_snooze",
        "report_preference", "statement_mode",
    }),
    "case_progress_ref": frozenset(
        {
            "case_hint",
            "progress_id",
            "replacement_summary",
            "replacement_details",
            "delete_reason",
            "start_at",
            "end_at",
        }
    ),
    "case_followup_policy": frozenset(
        {"case_hint", "cadence_type", "snoozed_until"}
    ),
    "knowledge_query": frozenset({"query", "topic"}),
    "report_event": frozenset({"report_type", "field"}),
    "periodic_report": frozenset({"report_type", "report_id", "period_key"}),
    "report_item_target": frozenset({"report_type", "replacement"}),
}


def validate_semantic_interpretation_contract(
    interpretation: SemanticInterpretation,
) -> None:
    """Validate the closed Runtime cognition vocabulary inside the LLM repair loop."""

    violations: list[str] = []
    for entity in interpretation.entities:
        allowed = ENTITY_ATTRIBUTE_KEYS.get(entity.entity_type)
        if allowed is None:
            violations.append(f"entities.{entity.entity_id}.entity_type")
            continue
        violations.extend(
            f"entities.{entity.entity_id}.attributes.{key}"
            for key in sorted(set(entity.attributes) - allowed)
        )
        reference = entity.attributes.get("context_reference")
        if reference is not None and not isinstance(reference, dict):
            violations.append(f"entities.{entity.entity_id}.attributes.context_reference")
        elif isinstance(reference, dict):
            violations.extend(
                f"entities.{entity.entity_id}.attributes.context_reference.{key}"
                for key in sorted(set(reference) - CONTEXT_REFERENCE_KEYS)
            )
            if any(not isinstance(value, str) for value in reference.values()):
                violations.append(
                    f"entities.{entity.entity_id}.attributes.context_reference.value_type"
                )
        for attribute_name in STRING_ENTITY_ATTRIBUTES.get(entity.entity_type, ()):
            value = entity.attributes.get(attribute_name)
            if value is not None and not isinstance(value, str):
                violations.append(
                    f"entities.{entity.entity_id}.attributes.{attribute_name}.value_type"
                )
        if entity.entity_type == "daily_report" and "version" in entity.attributes:
            version = entity.attributes.get("version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.version.value_type"
                )
        if entity.entity_type == "daily_report" and "items" in entity.attributes:
            replacement_items = entity.attributes.get("items")
            if (
                not isinstance(replacement_items, (list, tuple))
                or not replacement_items
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in replacement_items
                )
            ):
                violations.append(
                    f"entities.{entity.entity_id}.attributes.items.value_type"
                )
        if entity.entity_type == "case_progress_ref" and "expected_version" in entity.attributes:
            version = entity.attributes.get("expected_version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.expected_version.value_type"
                )
        if entity.entity_type == "travel_intent_ref":
            version = entity.attributes.get("expected_version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.expected_version.value_type"
                )
            new_status = entity.attributes.get("new_status")
            new_date_hint = str(entity.attributes.get("new_date_hint") or "").strip()
            if new_status not in {None, "", "cancelled"}:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.new_status.value"
                )
            if not new_date_hint and new_status != "cancelled":
                violations.append(
                    f"entities.{entity.entity_id}.attributes.requested_change"
                )
        if entity.entity_type == "case_followup_policy":
            cadence = entity.attributes.get("cadence_type")
            if cadence is not None and cadence not in {
                "daily", "weekly", "every_15_days", "monthly", "custom_interval",
                "event_only", "manual_only", "paused", "disabled",
            }:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.cadence_type.value"
                )
            interval = entity.attributes.get("custom_interval_days")
            if interval is not None and (
                not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0
            ):
                violations.append(
                    f"entities.{entity.entity_id}.attributes.custom_interval_days.value_type"
                )
            for flag in (
                "enabled", "hearing_reminders_enabled", "stage_transition_enabled",
                "node_transition_enabled",
            ):
                value = entity.attributes.get(flag)
                if value is not None and not isinstance(value, bool):
                    violations.append(
                        f"entities.{entity.entity_id}.attributes.{flag}.value_type"
                    )
            spans = entity.attributes.get("evidence_spans")
            if spans is not None and (
                not isinstance(spans, list)
                or any(
                    not isinstance(span, list)
                    or len(span) != 2
                    or any(
                        not isinstance(value, int) or isinstance(value, bool)
                        for value in span
                    )
                    for span in spans
                )
            ):
                violations.append(
                    f"entities.{entity.entity_id}.attributes.evidence_spans.value_type"
                )
        if entity.entity_type == "periodic_report" and "version" in entity.attributes:
            version = entity.attributes.get("version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.version.value_type"
                )
        if entity.entity_type in {"report_event", "periodic_report", "report_item_target"}:
            report_type = entity.attributes.get("report_type")
            if report_type not in {"weekly", "monthly"}:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.report_type.value"
                )
        if entity.entity_type == "report_item_target":
            target_ids = entity.attributes.get("target_item_ids")
            if target_ids is not None and (
                not isinstance(target_ids, list)
                or any(not isinstance(item, str) for item in target_ids)
            ):
                violations.append(
                    f"entities.{entity.entity_id}.attributes.target_item_ids.value_type"
                )
        if entity.entity_type == "case_progress_ref":
            for attribute_name in (
                "related_party_ids",
                "related_document_ids",
                "related_travel_intent_ids",
            ):
                value = entity.attributes.get(attribute_name)
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)
                ):
                    violations.append(
                        f"entities.{entity.entity_id}.attributes.{attribute_name}.value_type"
                    )
        if entity.entity_type == "daily_event":
            statement_mode = entity.attributes.get("statement_mode")
            if statement_mode is not None and statement_mode not in {
                "asserted",
                "question",
                "hypothetical",
                "quoted",
                "negated",
            }:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.statement_mode.value"
                )
        if entity.entity_type == "case_ref":
            for attribute_name in (
                "factual_progress", "completed_actions", "next_actions",
                "blocking_issues",
            ):
                value = entity.attributes.get(attribute_name)
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)
                ):
                    violations.append(
                        f"entities.{entity.entity_id}.attributes.{attribute_name}.value_type"
                    )
            time_scope = entity.attributes.get("action_time_scope")
            if time_scope is not None and time_scope not in {"today", "future", "unknown"}:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.action_time_scope.value"
                )
            preference = entity.attributes.get("report_preference")
            if preference is not None and preference not in {"automatic", "case_only", "ask"}:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.report_preference.value"
                )
            spans = entity.attributes.get("evidence_spans")
            if spans is not None and (
                not isinstance(spans, list)
                or any(
                    not isinstance(span, list)
                    or len(span) != 2
                    or any(not isinstance(value, int) or isinstance(value, bool) for value in span)
                    for span in spans
                )
            ):
                violations.append(
                    f"entities.{entity.entity_id}.attributes.evidence_spans.value_type"
                )
            statement_mode = entity.attributes.get("statement_mode")
            if statement_mode is not None and statement_mode not in {
                "asserted",
                "question",
                "hypothetical",
                "quoted",
                "negated",
            }:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.statement_mode.value"
                )
        if entity.entity_type == "travel_event":
            statement_mode = entity.attributes.get("statement_mode")
            if statement_mode is not None and statement_mode not in {
                "asserted",
                "question",
                "hypothetical",
                "quoted",
                "negated",
            }:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.statement_mode.value"
                )
            traveler_scope = entity.attributes.get("traveler_scope")
            if traveler_scope is not None and traveler_scope not in {
                "self",
                "other",
                "unknown",
            }:
                violations.append(
                    f"entities.{entity.entity_id}.attributes.traveler_scope.value"
                )
            spans = entity.attributes.get("evidence_spans")
            if spans is not None and (
                not isinstance(spans, list)
                or any(
                    not isinstance(span, list)
                    or len(span) != 2
                    or any(
                        not isinstance(value, int) or isinstance(value, bool)
                        for value in span
                    )
                    for span in spans
                )
            ):
                violations.append(
                    f"entities.{entity.entity_id}.attributes.evidence_spans.value_type"
                )
    for action in interpretation.required_actions:
        allowed_parameters = ACTION_PARAMETER_KEYS.get(action.action_type)
        if allowed_parameters is None:
            violations.append(f"actions.{action.action_id}.action_type")
            continue
        violations.extend(
            f"actions.{action.action_id}.parameters.{key}"
            for key in sorted(set(action.parameters) - allowed_parameters)
        )
        if any(not isinstance(value, str) for value in action.parameters.values()):
            violations.append(f"actions.{action.action_id}.parameters.value_type")
    _validate_ambiguous_case_alias_contract(interpretation, violations)
    if violations:
        raise ValueError(
            "semantic interpretation violates closed Runtime contract: "
            + ", ".join(violations)
        )


def _validate_ambiguous_case_alias_contract(
    interpretation: SemanticInterpretation,
    violations: list[str],
) -> None:
    """Require cognition to preserve the proposed Case action across selection.

    Admission may authorize or block an action, but it must never invent one.  An
    ambiguous alias therefore carries the user's original semantic proposal as a
    fully bound, still non-executable action.  Trusted Admission can then turn the
    ambiguity into a SelectionRequest without reconstructing business meaning.
    """

    clarification = interpretation.clarification_need
    if clarification is None or clarification.reason != "ambiguous_case_alias":
        return
    prefix = "clarification_need.ambiguous_case_alias"
    if clarification.missing_fields != ("case_id",):
        violations.append(f"{prefix}.missing_fields")

    actions = [
        action
        for action in interpretation.required_actions
        if action.action_type == "record_case_progress"
    ]
    if len(actions) != 1:
        violations.append(f"{prefix}.record_case_progress_action")
        return
    action = actions[0]
    if action.intent != "case_progress":
        violations.append(f"actions.{action.action_id}.intent")
    if len(action.entity_ids) != 1:
        violations.append(f"actions.{action.action_id}.case_ref_binding")
        return

    entity_by_id = {entity.entity_id: entity for entity in interpretation.entities}
    entity = entity_by_id.get(action.entity_ids[0])
    if entity is None or entity.entity_type != "case_ref":
        violations.append(f"actions.{action.action_id}.case_ref_binding")

    bound_segments = [
        segment
        for segment in interpretation.segments
        if action.action_id in segment.action_ids
    ]
    if len(bound_segments) != 1:
        violations.append(f"actions.{action.action_id}.exact_segment_binding")
        return
    segment = bound_segments[0]
    if (
        action.intent not in segment.intents
        or action.entity_ids[0] not in segment.entity_ids
    ):
        violations.append(f"actions.{action.action_id}.exact_segment_binding")
