from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol

from app.agent2.cognitive_core_v3 import (
    CognitiveTurn,
    SemanticInputLimitExceeded,
    SemanticInterpretation,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.cognitive_contract_v3 import validate_semantic_interpretation_contract
from app.agent2.case_statement_contract import assess_case_progress_statement
from app.agent2.business.case_reference import (
    discover_visible_case_references,
    normalize_case_reference,
)
from app.agent2.business.travel import resolve_travel_window
from app.agent2.oracle_guard import assert_no_oracle_fields
from app.agent2.report_document_contract import (
    StructuredDailyDocument,
    StructuredReportItem,
    complete_daily_document_replace_semantic_payload,
    daily_compound_section_correction_semantic_payload,
    daily_item_section_correction_semantic_payload,
    daily_section_cue_semantic_payload,
    parse_structured_daily_document,
    structured_daily_semantic_payload,
)
from app.utils.json import extract_json_object


PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "cognitive_core_v3.md"
MAX_COGNITIVE_INPUT_CHARS = 2000


class StructuredCompletionClient(Protocol):
    async def complete_json(self, **kwargs: Any) -> str: ...


class LLMCognitiveSemanticInterpreter:
    """LLM adapter at the semantic seam; output is validated as cognition only."""

    def __init__(
        self,
        client: StructuredCompletionClient,
        *,
        model: str | None = None,
        thinking_enabled: bool = False,
        legacy_semantic_enforcers_enabled: bool = True,
    ):
        self._client = client
        self._model = model
        self._thinking_enabled = thinking_enabled
        self._legacy_semantic_enforcers_enabled = bool(
            legacy_semantic_enforcers_enabled
        )
        self._prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
        self._used_sources: dict[str, str] = {}

    def source_for(self, message_id: str) -> str:
        """Expose the semantic path for deterministic replay evidence."""

        return self._used_sources.get(str(message_id or ""), "")

    def runtime_identity(self) -> dict[str, Any]:
        """Return non-secret execution configuration that affects semantics."""

        return {
            "adapter": f"{type(self).__module__}.{type(self).__qualname__}",
            "model": self._model or "client-default",
            "thinking_enabled": self._thinking_enabled,
            "action_free_named_case_reassessment": True,
            "legacy_semantic_enforcers_enabled": (
                self._legacy_semantic_enforcers_enabled
            ),
        }

    async def interpret(self, turn: CognitiveTurn, state: ConversationState) -> SemanticInterpretation:
        if len(turn.text) > MAX_COGNITIVE_INPUT_CHARS:
            raise SemanticInputLimitExceeded(
                f"cognitive input exceeds {MAX_COGNITIVE_INPUT_CHARS} characters"
            )
        assert_no_oracle_fields(turn.resources, path="turn.resources")
        assert_no_oracle_fields(
            _conversation_state_oracle_guard_payload(state),
            path="conversation_state",
        )
        structured_daily_document = parse_structured_daily_document(turn.text)
        deterministic_payload = (
            _report_meta_opening_semantic_payload(turn.text)
            or daily_compound_section_correction_semantic_payload(
                turn.text,
                turn.resources,
            )
            or daily_item_section_correction_semantic_payload(
                turn.text,
                turn.resources,
            )
            or daily_section_cue_semantic_payload(turn.text, turn.resources)
            or complete_daily_document_replace_semantic_payload(
                turn.text,
                turn.resources,
                document=structured_daily_document,
            )
            or structured_daily_semantic_payload(turn.text)
            or _active_daily_plain_content_semantic_payload(turn=turn, state=state)
        )
        structured_daily_requires_case_facet = bool(
            structured_daily_document is not None
            and not self._legacy_semantic_enforcers_enabled
            and any(
                discover_visible_case_references(
                    item.value,
                    turn.resources.get("visible_cases"),
                )
                for item in structured_daily_document.items
            )
        )
        if (
            deterministic_payload is not None
            and not structured_daily_requires_case_facet
        ):
            self._used_sources[str(turn.message_id)] = "deterministic_contract"
            interpretation = SemanticInterpretation.from_payload(deterministic_payload)
            validate_semantic_interpretation_contract(interpretation)
            _validate_turn_contract(turn, state, interpretation)
            return interpretation
        base_prompt = _build_semantic_base_prompt(
            self._prompt_template,
            turn=turn,
            state=state,
        )
        self._used_sources[str(turn.message_id)] = "live_model"
        output = ""
        validation_error: ValueError | None = None
        for attempt in range(3):
            user_prompt = base_prompt
            if attempt:
                user_prompt += (
                    "\n\nThe previous JSON violated the cognition schema. Repair the semantic "
                    "contract only; add and bind a required semantic action when the validation "
                    "error requires it, but do not add execution fields.\nValidation error: "
                    + str(validation_error or "invalid semantic output")
                    + "\nPrevious JSON:\n"
                    + output
                )
            output = await self._client.complete_json(
                system_prompt=(
                    "You are Agent2 Cognitive Core v3. Return strict JSON cognition only. "
                    "Never return commands, effects, allow_write, should_write_db, SQL, or database operations."
                ),
                user_prompt=user_prompt,
                model=self._model,
                thinking_enabled=self._thinking_enabled,
            )
            try:
                candidate = extract_json_object(output)
                if self._legacy_semantic_enforcers_enabled:
                    candidate = _apply_legacy_semantic_enforcers(
                        candidate,
                        turn=turn,
                        state=state,
                    )
                candidate = (
                    structured_daily_semantic_payload(turn.text)
                    or candidate
                )
                candidate = _normalize_incomplete_case_target_to_clarification(
                    candidate,
                )
                candidate = _normalize_overlapping_case_mutations(candidate)
                candidate = _normalize_new_case_progress_case_hint(
                    candidate,
                    turn=turn,
                )
                candidate = _normalize_asserted_case_progress_statement_mode(
                    candidate,
                    turn=turn,
                )
                candidate = _normalize_asserted_case_progress_source_fact(candidate)
                candidate = _normalize_case_evidence_spans(candidate)
                interpretation = SemanticInterpretation.from_payload(candidate)
                validate_semantic_interpretation_contract(interpretation)
                _validate_turn_contract(turn, state, interpretation)
                interpretation = _normalize_explicit_travel_runtime_attributes(
                    interpretation,
                    turn=turn,
                )
                validate_semantic_interpretation_contract(interpretation)
                _validate_turn_contract(turn, state, interpretation)
                if not self._legacy_semantic_enforcers_enabled:
                    if structured_daily_document is not None:
                        supplements = await self._reassess_structured_daily_case_items(
                            document=structured_daily_document,
                            turn=turn,
                            state=state,
                        )
                        for supplemental in supplements:
                            merged = _merge_independent_interpretations(
                                interpretation,
                                supplemental,
                            )
                            validate_semantic_interpretation_contract(merged)
                            _validate_turn_contract(turn, state, merged)
                            interpretation = merged
                    elif _should_reassess_missing_named_case_facet(
                        interpretation,
                        turn=turn,
                    ):
                        reassessed = await self._reassess_action_free_named_case(
                            base_prompt=base_prompt,
                            previous_output=output,
                            turn=turn,
                            state=state,
                        )
                        if reassessed is not None:
                            merged = _merge_independent_interpretations(
                                interpretation,
                                reassessed,
                            )
                            validate_semantic_interpretation_contract(merged)
                            _validate_turn_contract(turn, state, merged)
                            interpretation = merged
                interpretation = _project_case_travel_plan_to_daily_facet(
                    interpretation,
                    turn=turn,
                )
                validate_semantic_interpretation_contract(interpretation)
                _validate_turn_contract(turn, state, interpretation)
                return interpretation
            except ValueError as exc:
                validation_error = exc
        raise validation_error or ValueError("invalid cognitive semantic output")

    async def _reassess_action_free_named_case(
        self,
        *,
        base_prompt: str,
        previous_output: str,
        turn: CognitiveTurn,
        state: ConversationState,
    ) -> SemanticInterpretation | None:
        """Give one focused semantic retry without deriving a mutation by keyword.

        Production Admission intentionally consumes the raw model proposal. When
        that proposal omits the Case facet despite an exact, authorized Case
        reference, a second semantic pass distinguishes a Case-work assertion
        from a question, service request, example, hypothetical, or opt-out.
        The retry is accepted only as one grounded ``record_case_progress``
        proposal; all execution authority remains with Admission and receipts.
        """

        authorized_matches = _authorized_case_reference_matches(
            turn.text,
            turn.resources,
        )
        if not authorized_matches:
            return None
        authorized_references = tuple(
            str(item["reference"]) for item in authorized_matches
        )
        ambiguous_reference = len(authorized_matches) > 1
        validation_references = (
            authorized_references
            if ambiguous_reference
            else tuple(authorized_matches[0]["trusted_references"])
        )
        reference_context: object
        if ambiguous_reference:
            reference_context = [
                {
                    "case_number": item["case_number"],
                    "case_name": item["case_name"],
                    "matched_reference": item["reference"],
                    "version": item["version"],
                }
                for item in authorized_matches
            ]
        else:
            reference_context = authorized_references[0]
        reassessment_prompt = (
            base_prompt
            + "\n\nFocused Case-work reassessment:\n"
            + "The previous JSON was valid but did not include a Case-progress action, "
            + "while the source text "
            + (
                "contains one Case reference that matches multiple trusted visible "
                "Cases: "
                if ambiguous_reference
                else "contains exactly one trusted visible Case reference: "
            )
            + json.dumps(reference_context, ensure_ascii=False)
            + ". Reassess the full source turn semantically. Case progress includes "
            + "completed, ongoing, failed, blocked, or planned work tied to that Case, "
            + "including coordination with branches, checking materials, locating local "
            + "resources, engaging local counsel, evidence work, court communication, "
            + "hearing work, investigation, enforcement, and other legal work. If this is "
            + "an asserted Case fact or work update, return exactly one grounded "
            + "record_case_progress action, put the visible Case reference in "
            + "case_ref.value, set statement_mode=asserted, preserve the exact source "
            + "text/evidence, and never use case_hint on case_ref. "
            + (
                "Because multiple trusted Cases match, do not choose one. Also return "
                "clarification_need.reason=ambiguous_case_alias, missing_fields "
                "[case_id], and a question that lists the trusted Case "
                "numbers/names so the user can select one; the grounded action must "
                "remain bound to the source segment so Selection Pending can retain "
                "the original fact without writing it yet. "
                if ambiguous_reference
                else ""
            )
            + "If it is a question, "
            + "request for the assistant to perform work, example, hypothetical, quote, "
            + "or explicit opt-out, keep it non-mutating. Return the complete strict JSON "
            + "contract, not an explanation.\nPrevious JSON:\n"
            + previous_output
        )
        try:
            output = await self._client.complete_json(
                system_prompt=(
                    "You are Agent2 Cognitive Core v3 performing one focused Case-work "
                    "semantic reassessment. Return strict JSON cognition only."
                ),
                user_prompt=reassessment_prompt,
                model=self._model,
                thinking_enabled=self._thinking_enabled,
            )
            candidate = extract_json_object(output)
            candidate = _retain_only_supplemental_case_progress(candidate)
            candidate = _scope_supplemental_case_to_structured_daily_item(
                candidate,
                turn=turn,
            )
            candidate = _normalize_new_case_progress_case_hint(
                candidate,
                turn=turn,
            )
            candidate = _normalize_asserted_case_progress_statement_mode(
                candidate,
                turn=turn,
            )
            candidate = _normalize_asserted_case_progress_source_fact(candidate)
            candidate = _normalize_case_evidence_spans(candidate)
            interpretation = SemanticInterpretation.from_payload(candidate)
            validate_semantic_interpretation_contract(interpretation)
            _validate_turn_contract(turn, state, interpretation)
            if not _is_grounded_case_progress_reassessment(
                interpretation,
                authorized_references=validation_references,
                ambiguous_reference=ambiguous_reference,
            ):
                return None
            return interpretation
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    async def _reassess_structured_daily_case_items(
        self,
        *,
        document: StructuredDailyDocument,
        turn: CognitiveTurn,
        state: ConversationState,
    ) -> tuple[SemanticInterpretation, ...]:
        """Reassess Case-bearing Daily items independently and in source order."""

        supplements: list[SemanticInterpretation] = []
        for index, item in enumerate(document.items, start=1):
            item_turn = replace(turn, text=item.value)
            if not _authorized_case_reference_matches(
                item_turn.text,
                item_turn.resources,
            ):
                continue
            item_base_prompt = _build_semantic_base_prompt(
                self._prompt_template,
                turn=item_turn,
                state=state,
            )
            reassessed = await self._reassess_action_free_named_case(
                base_prompt=item_base_prompt,
                previous_output=_action_free_item_output(item.value, index=index),
                turn=item_turn,
                state=state,
            )
            if reassessed is None:
                continue
            supplements.append(
                _namespace_structured_daily_case_supplement(
                    reassessed,
                    index=index,
                    item=item,
                )
            )
        return tuple(supplements)


def _build_semantic_base_prompt(
    prompt_template: str,
    *,
    turn: CognitiveTurn,
    state: ConversationState,
) -> str:
    payload = {
        "turn": {
            "user_id": turn.user_id,
            "conversation_id": turn.conversation_id,
            "message_id": turn.message_id,
            "text": turn.text,
            "occurred_at": turn.occurred_at.isoformat(),
            "resources": dict(turn.resources),
        },
        "conversation_state": _state_payload(state),
    }
    return prompt_template.replace(
        "{{payload_json}}",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _action_free_item_output(text: str, *, index: int) -> str:
    return json.dumps(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": f"structured-daily-item-{index}-prior",
                    "text": text,
                    "intents": ["chat"],
                    "entity_ids": [],
                    "action_ids": [],
                }
            ],
            "entities": [],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {
                "preserve_current_goal": True,
                "remember_turn": True,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _namespace_structured_daily_case_supplement(
    interpretation: SemanticInterpretation,
    *,
    index: int,
    item: StructuredReportItem,
) -> SemanticInterpretation:
    prefix = f"structured-daily-case-{index}"
    entity_id_map = {
        entity.entity_id: f"{prefix}-entity-{position}"
        for position, entity in enumerate(interpretation.entities, start=1)
    }
    action_id_map = {
        action.action_id: f"{prefix}-action-{position}"
        for position, action in enumerate(interpretation.required_actions, start=1)
    }
    entities = tuple(
        replace(
            entity,
            entity_id=entity_id_map[entity.entity_id],
            attributes={
                **entity.attributes,
                "normalized_fact": item.value,
                "evidence_spans": [[0, len(item.value)]],
            },
        )
        for entity in interpretation.entities
    )
    actions = tuple(
        replace(
            action,
            action_id=action_id_map[action.action_id],
            entity_ids=tuple(entity_id_map[value] for value in action.entity_ids),
        )
        for action in interpretation.required_actions
    )
    segments = tuple(
        replace(
            segment,
            segment_id=f"{prefix}-segment-{position}",
            text=item.value,
            text_hash=_sha256_text(item.value),
            entity_ids=tuple(entity_id_map[value] for value in segment.entity_ids),
            action_ids=tuple(action_id_map[value] for value in segment.action_ids),
            start_offset=item.start_offset,
            end_offset=item.end_offset,
        )
        for position, segment in enumerate(interpretation.segments, start=1)
    )
    update = interpretation.context_update
    pending = update.bind_pending
    if pending is not None:
        pending = replace(
            pending,
            entity_ids=tuple(entity_id_map[value] for value in pending.entity_ids),
        )
    return replace(
        interpretation,
        segments=segments,
        entities=entities,
        required_actions=actions,
        context_update=replace(
            update,
            remember_entity_ids=tuple(
                entity_id_map[value] for value in update.remember_entity_ids
            ),
            bind_pending=pending,
        ),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _should_reassess_missing_named_case_facet(
    interpretation: SemanticInterpretation,
    *,
    turn: CognitiveTurn,
) -> bool:
    if interpretation.clarification_need is not None:
        return False
    if any(
        action.action_type
        in {
            "record_case_progress",
            "update_case_progress",
            "delete_case_progress",
            "query_case_progress",
            "link_case_progress",
            "answer_case_query",
        }
        for action in interpretation.required_actions
    ):
        return False
    # The focused retry, not this routing guard, distinguishes a Case-work
    # assertion from a question, service request, example, or opt-out.  Keeping
    # those turns in the same semantic pass prevents a brittle local heuristic
    # from both swallowing uncommon work updates and turning requests such as
    # "帮我找当地资源" into progress writes.
    return bool(_authorized_case_reference_matches(turn.text, turn.resources))


def _retain_only_supplemental_case_progress(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Keep the one requested Case facet when a focused model repeats siblings.

    The focused retry is supplemental by contract.  Some models nevertheless
    echo the already accepted Travel action and trusted Case IDs from resources.
    Removing those echoes is safe: it cannot create a Case action, choose an ID,
    or change the source fact; the remaining Case reference must still pass the
    normal grounding and admission checks.
    """

    actions = payload.get("required_actions")
    entities = payload.get("entities")
    segments = payload.get("segments")
    if not all(isinstance(value, list) for value in (actions, entities, segments)):
        return payload
    case_actions = [
        action
        for action in actions
        if isinstance(action, dict)
        and action.get("action_type") == "record_case_progress"
    ]
    if len(case_actions) != 1:
        return payload
    case_action = dict(case_actions[0])
    case_action_id = str(case_action.get("action_id") or "")
    entity_ids = case_action.get("entity_ids")
    if (
        not case_action_id
        or not isinstance(entity_ids, list)
        or len(entity_ids) != 1
        or not str(entity_ids[0] or "")
    ):
        return payload
    case_entity_id = str(entity_ids[0])
    case_entities = [
        entity
        for entity in entities
        if isinstance(entity, dict)
        and str(entity.get("entity_id") or "") == case_entity_id
        and entity.get("entity_type") == "case_ref"
    ]
    if len(case_entities) != 1:
        return payload
    case_entity = dict(case_entities[0])
    attributes = case_entity.get("attributes")
    if isinstance(attributes, dict):
        normalized_attributes = dict(attributes)
        for forbidden_identity in (
            "case_id",
            "case_number",
            "external_case_id",
            "version",
        ):
            normalized_attributes.pop(forbidden_identity, None)
        case_entity["attributes"] = normalized_attributes

    case_segments: list[dict[str, Any]] = []
    for raw_segment in segments:
        if not isinstance(raw_segment, dict):
            continue
        action_ids = raw_segment.get("action_ids")
        if not isinstance(action_ids, list) or case_action_id not in {
            str(value) for value in action_ids
        }:
            continue
        segment = dict(raw_segment)
        segment["intents"] = ["case_progress"]
        segment["entity_ids"] = [case_entity_id]
        segment["action_ids"] = [case_action_id]
        case_segments.append(segment)
    if not case_segments:
        return payload

    context_update = dict(payload.get("context_update") or {})
    context_update["current_goal"] = "case_progress"
    context_update["preserve_current_goal"] = False
    context_update["remember_entity_ids"] = [case_entity_id]
    context_update["remember_turn"] = True
    return {
        "intents": ["case_progress"],
        "segments": case_segments,
        "entities": [case_entity],
        "confidence": payload.get("confidence", 0.0),
        "required_actions": [case_action],
        "clarification_need": payload.get("clarification_need"),
        "context_update": context_update,
    }


def _scope_supplemental_case_to_structured_daily_item(
    payload: dict[str, Any],
    *,
    turn: CognitiveTurn,
) -> dict[str, Any]:
    """Bind a focused Case supplement to its exact structured Daily item.

    Providers commonly return the whole Daily document as the Case action's
    semantic segment even when the extracted Case fact is item-specific.  The
    full document must never become a CaseProgress body.  Trusted parsing can
    narrow the segment only when exactly one report item contains a visible,
    permission-scoped Case reference; otherwise the original payload remains
    unchanged and the normal fail-closed validation path applies.
    """

    document = parse_structured_daily_document(turn.text)
    if document is None:
        return payload
    visible_cases = turn.resources.get("visible_cases")
    matched_items = [
        item
        for item in document.items
        if discover_visible_case_references(item.value, visible_cases)
    ]
    if len(matched_items) != 1:
        return payload

    actions = payload.get("required_actions")
    segments = payload.get("segments")
    if not isinstance(actions, list) or not isinstance(segments, list):
        return payload
    case_actions = [
        item
        for item in actions
        if isinstance(item, dict)
        and item.get("action_type") == "record_case_progress"
        and str(item.get("action_id") or "")
    ]
    if len(case_actions) != 1:
        return payload
    action_id = str(case_actions[0]["action_id"])
    bound_segments = [
        item
        for item in segments
        if isinstance(item, dict)
        and isinstance(item.get("action_ids"), list)
        and action_id in {str(value) for value in item["action_ids"]}
    ]
    if len(bound_segments) != 1:
        return payload

    source_item = matched_items[0]
    scoped_segment = dict(bound_segments[0])
    scoped_segment["text"] = source_item.value
    scoped_segment["start_offset"] = source_item.start_offset
    scoped_segment["end_offset"] = source_item.end_offset
    result = dict(payload)
    result["segments"] = [scoped_segment]
    return result


def _project_case_travel_plan_to_daily_facet(
    interpretation: SemanticInterpretation,
    *,
    turn: CognitiveTurn,
) -> SemanticInterpretation:
    """Project one explicit future self trip into the report-plan candidate.

    Daily is an independent projection facet rather than an exclusive dialogue
    domain.  A closed-form future Travel assertion may therefore contribute to
    ``tomorrow_plan`` even when Daily is not the active conversation goal.  The
    projection still emits only a semantic candidate for the normal Report
    admission/executor path.
    """

    if interpretation.clarification_need is not None:
        return interpretation
    action_types = {
        action.action_type for action in interpretation.required_actions
    }
    if (
        "record_travel_event" not in action_types
        or "capture_daily_event" in action_types
    ):
        return interpretation
    entities_by_id = {entity.entity_id: entity for entity in interpretation.entities}
    travel_actions = [
        action
        for action in interpretation.required_actions
        if action.action_type == "record_travel_event"
    ]
    if len(travel_actions) != 1:
        return interpretation
    travel_action = travel_actions[0]
    if len(travel_action.entity_ids) != 1:
        return interpretation
    travel_entity = entities_by_id.get(travel_action.entity_ids[0])
    extracted_travel = _extract_explicit_travel_event(turn.text)
    trusted_model_assertion = bool(
        travel_entity is not None
        and str(travel_entity.attributes.get("statement_mode") or "") == "asserted"
        and str(travel_entity.attributes.get("traveler_scope") or "") == "self"
    )
    if (
        travel_entity is None
        or travel_entity.entity_type != "travel_event"
        or (
            extracted_travel is None
            and not trusted_model_assertion
        )
    ):
        return interpretation
    shared_segments = [
        segment
        for segment in interpretation.segments
        if travel_action.action_id in segment.action_ids
        and segment.text in turn.text
    ]
    if len(shared_segments) != 1:
        return interpretation
    source_segment = shared_segments[0]
    date_hint = str(
        (extracted_travel or {}).get("date_hint")
        or travel_entity.attributes.get("date_hint")
        or ""
    ).strip()
    if date_hint not in {
        "tomorrow",
        "day_after_tomorrow",
        "next_monday",
        "明天",
        "明日",
        "后天",
        "下周一",
    }:
        try:
            travel_window = resolve_travel_window(
                str(travel_entity.value or source_segment.text),
                reference_date=turn.occurred_at.date(),
            )
        except ValueError:
            return interpretation
        if travel_window.start_date <= turn.occurred_at.date():
            return interpretation

    supplemental = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "case-travel-report-plan-segment",
                    "text": source_segment.text,
                    "start_offset": source_segment.start_offset,
                    "end_offset": source_segment.end_offset,
                    "intents": ["daily_append"],
                    "entity_ids": ["case-travel-report-plan-event"],
                    "action_ids": ["capture-case-travel-report-plan"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-travel-report-plan-event",
                    "entity_type": "daily_event",
                    "value": source_segment.text,
                    "confidence": travel_entity.confidence,
                    "attributes": {"field": "tomorrow_plan"},
                }
            ],
            "confidence": min(interpretation.confidence, travel_entity.confidence),
            "required_actions": [
                {
                    "action_id": "capture-case-travel-report-plan",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["case-travel-report-plan-event"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "preserve_current_goal": True,
                "remember_entity_ids": ["case-travel-report-plan-event"],
                "remember_turn": True,
            },
        }
    )
    return _merge_independent_interpretations(interpretation, supplemental)


def _normalize_explicit_travel_runtime_attributes(
    interpretation: SemanticInterpretation,
    *,
    turn: CognitiveTurn,
) -> SemanticInterpretation:
    """Narrow a validated explicit trip to the closed Runtime attributes.

    Schema-invalid model output must first take the existing repair path.  Only
    after semantic validation succeeds may an exact source-text trip discard
    descriptive provider metadata that the Runtime does not consume.
    """

    extracted = _extract_explicit_travel_event(turn.text)
    if extracted is None:
        return interpretation
    travel_actions = [
        action
        for action in interpretation.required_actions
        if action.action_type == "record_travel_event"
    ]
    if len(travel_actions) != 1 or len(travel_actions[0].entity_ids) != 1:
        return interpretation
    entity_id = travel_actions[0].entity_ids[0]
    matching_entities = [
        entity
        for entity in interpretation.entities
        if entity.entity_id == entity_id and entity.entity_type == "travel_event"
    ]
    if len(matching_entities) != 1:
        return interpretation
    permitted_keys = {"destination", "date_hint", "purpose"}
    normalized_entities = tuple(
        replace(
            entity,
            value=str(turn.text or "").strip(),
            attributes={
                key: value
                for key, value in entity.attributes.items()
                if key in permitted_keys
            },
        )
        if entity.entity_id == entity_id
        else entity
        for entity in interpretation.entities
    )
    return replace(interpretation, entities=normalized_entities)


def _merge_independent_interpretations(
    primary: SemanticInterpretation,
    supplemental: SemanticInterpretation,
) -> SemanticInterpretation:
    """Add one independently validated semantic facet without dropping siblings."""

    primary_entity_ids = {entity.entity_id for entity in primary.entities}
    supplemental_entity_ids = {entity.entity_id for entity in supplemental.entities}
    primary_action_ids = {action.action_id for action in primary.required_actions}
    supplemental_action_ids = {action.action_id for action in supplemental.required_actions}
    if primary_entity_ids & supplemental_entity_ids:
        return primary
    if primary_action_ids & supplemental_action_ids:
        return primary

    segments = list(primary.segments)
    for incoming in supplemental.segments:
        matching_index = next(
            (
                index
                for index, existing in enumerate(segments)
                if existing.text == incoming.text
                and existing.start_offset == incoming.start_offset
                and existing.end_offset == incoming.end_offset
            ),
            None,
        )
        if matching_index is None:
            if any(existing.segment_id == incoming.segment_id for existing in segments):
                return primary
            segments.append(incoming)
            continue
        existing = segments[matching_index]
        segments[matching_index] = replace(
            existing,
            intents=tuple(dict.fromkeys((*existing.intents, *incoming.intents))),
            entity_ids=tuple(
                dict.fromkeys((*existing.entity_ids, *incoming.entity_ids))
            ),
            action_ids=tuple(
                dict.fromkeys((*existing.action_ids, *incoming.action_ids))
            ),
        )

    primary_update = primary.context_update
    supplemental_update = supplemental.context_update
    combined_action_types = {
        action.action_type
        for action in (*primary.required_actions, *supplemental.required_actions)
    }
    merged_goal = (
        "case_progress"
        if combined_action_types.intersection(
            {
                "record_case_progress",
                "update_case_progress",
                "delete_case_progress",
                "query_case_progress",
                "link_case_progress",
                "answer_case_query",
            }
        )
        else (
            "travel_event"
            if combined_action_types.intersection(
                {"record_travel_event", "respond_travel_collaboration"}
            )
            else (supplemental_update.current_goal or primary_update.current_goal)
        )
    )
    return SemanticInterpretation(
        intents=tuple(dict.fromkeys((*primary.intents, *supplemental.intents))),
        segments=tuple(segments),
        entities=(*primary.entities, *supplemental.entities),
        confidence=min(primary.confidence, supplemental.confidence),
        required_actions=(
            *primary.required_actions,
            *supplemental.required_actions,
        ),
        clarification_need=(
            supplemental.clarification_need or primary.clarification_need
        ),
        context_update=replace(
            primary_update,
            current_goal=merged_goal,
            preserve_current_goal=(
                False if merged_goal else (
                    primary_update.preserve_current_goal
                    or supplemental_update.preserve_current_goal
                )
            ),
            remember_entity_ids=tuple(
                dict.fromkeys(
                    (
                        *primary_update.remember_entity_ids,
                        *supplemental_update.remember_entity_ids,
                    )
                )
            ),
            remember_turn=(
                primary_update.remember_turn or supplemental_update.remember_turn
            ),
            bind_pending=(
                supplemental_update.bind_pending or primary_update.bind_pending
            ),
            consumed_pending_ids=tuple(
                dict.fromkeys(
                    (
                        *primary_update.consumed_pending_ids,
                        *supplemental_update.consumed_pending_ids,
                    )
                )
            ),
            resume_previous_goal=(
                primary_update.resume_previous_goal
                or supplemental_update.resume_previous_goal
            ),
            clear_current_goal=(
                primary_update.clear_current_goal
                or supplemental_update.clear_current_goal
            ),
        ),
    )


def _is_grounded_case_progress_reassessment(
    interpretation: SemanticInterpretation,
    *,
    authorized_references: tuple[str, ...],
    ambiguous_reference: bool,
) -> bool:
    actions = tuple(interpretation.required_actions)
    if len(actions) != 1 or actions[0].action_type != "record_case_progress":
        return False
    if len(actions[0].entity_ids) != 1:
        return False
    entity = next(
        (
            item
            for item in interpretation.entities
            if item.entity_id == actions[0].entity_ids[0]
        ),
        None,
    )
    if entity is None or entity.entity_type != "case_ref":
        return False
    if entity.attributes.get("statement_mode") != "asserted":
        return False
    if _normalize_case_reference(entity.value) not in {
        _normalize_case_reference(item) for item in authorized_references
    }:
        return False
    clarification = interpretation.clarification_need
    if ambiguous_reference:
        return bool(
            clarification is not None
            and clarification.reason == "ambiguous_case_alias"
            and clarification.missing_fields == ("case_id",)
        )
    return clarification is None


def _apply_legacy_semantic_enforcers(
    candidate: dict[str, Any],
    *,
    turn: CognitiveTurn,
    state: ConversationState,
) -> dict[str, Any]:
    """Compatibility baseline used by Disabled/Shadow; Enforce bypasses it."""

    return _enforce_operation_status_query(
        _enforce_authorized_case_progress(
            _enforce_cross_domain_context_fencing(
                _enforce_explicit_travel_event(
                    _enforce_daily_deictic_projection(
                        _enforce_report_task_exit(
                            _enforce_report_meta_opening(
                                _enforce_non_unique_short_confirmation(
                                    candidate,
                                    text=turn.text,
                                    state=state,
                                    occurred_at=turn.occurred_at,
                                ),
                                text=turn.text,
                            ),
                            text=turn.text,
                            state=state,
                        ),
                        text=turn.text,
                        resources=turn.resources,
                        state=state,
                        occurred_at=turn.occurred_at,
                    ),
                    text=turn.text,
                ),
                text=turn.text,
                state=state,
            ),
            text=turn.text,
            resources=turn.resources,
        ),
        text=turn.text,
    )


def _normalize_case_evidence_spans(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize one safe JSON shape without changing evidence coordinates."""

    entities = payload.get("entities")
    if not isinstance(entities, list):
        return payload
    normalized_entities: list[Any] = []
    changed = False
    for raw_entity in entities:
        if not isinstance(raw_entity, dict) or raw_entity.get("entity_type") != "case_ref":
            normalized_entities.append(raw_entity)
            continue
        attributes = raw_entity.get("attributes")
        if not isinstance(attributes, dict):
            normalized_entities.append(raw_entity)
            continue
        spans = attributes.get("evidence_spans")
        if not isinstance(spans, list):
            normalized_entities.append(raw_entity)
            continue
        normalized_spans: list[Any] = []
        entity_changed = False
        for span in spans:
            if (
                isinstance(span, dict)
                and set(span) == {"start", "end"}
                and all(
                    isinstance(span[key], int) and not isinstance(span[key], bool)
                    for key in ("start", "end")
                )
            ):
                normalized_spans.append([span["start"], span["end"]])
                entity_changed = True
            else:
                normalized_spans.append(span)
        if not entity_changed:
            normalized_entities.append(raw_entity)
            continue
        normalized_entity = dict(raw_entity)
        normalized_attributes = dict(attributes)
        normalized_attributes["evidence_spans"] = normalized_spans
        normalized_entity["attributes"] = normalized_attributes
        normalized_entities.append(normalized_entity)
        changed = True
    if not changed:
        return payload
    result = dict(payload)
    result["entities"] = normalized_entities
    return result


def _normalize_asserted_case_progress_source_fact(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Make the exact user segment authoritative for an asserted Case fact.

    The model may classify and structure the action, but it may not paraphrase
    the business body that will later be written or retained by Selection
    Pending.  This normalization is limited to already-asserted
    ``record_case_progress`` actions and therefore does not create a write.
    """

    raw_entities = payload.get("entities")
    raw_actions = payload.get("required_actions")
    raw_segments = payload.get("segments")
    if not all(
        isinstance(value, list)
        for value in (raw_entities, raw_actions, raw_segments)
    ):
        return payload
    entity_ids_by_action = {
        str(action.get("action_id") or ""): tuple(
            str(value) for value in action.get("entity_ids", ())
        )
        for action in raw_actions
        if isinstance(action, dict)
        and action.get("action_type") == "record_case_progress"
        and isinstance(action.get("entity_ids"), list)
        and str(action.get("action_id") or "")
    }
    segments_by_entity: dict[str, list[str]] = {}
    for segment in raw_segments:
        if not isinstance(segment, dict) or not isinstance(
            segment.get("action_ids"), list
        ):
            continue
        segment_text = str(segment.get("text") or "")
        if not segment_text:
            continue
        for action_id in segment["action_ids"]:
            for entity_id in entity_ids_by_action.get(str(action_id), ()):
                segments_by_entity.setdefault(entity_id, []).append(segment_text)

    normalized_entities: list[Any] = []
    changed = False
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            normalized_entities.append(raw_entity)
            continue
        entity_id = str(raw_entity.get("entity_id") or "")
        attributes = raw_entity.get("attributes")
        segment_texts = tuple(dict.fromkeys(segments_by_entity.get(entity_id, ())))
        if (
            raw_entity.get("entity_type") != "case_ref"
            or not isinstance(attributes, dict)
            or str(attributes.get("statement_mode") or "") != "asserted"
            or len(segment_texts) != 1
        ):
            normalized_entities.append(raw_entity)
            continue
        source_fact = segment_texts[0]
        normalized_entity = dict(raw_entity)
        normalized_attributes = dict(attributes)
        normalized_attributes["normalized_fact"] = source_fact
        normalized_attributes["evidence_spans"] = [[0, len(source_fact)]]
        normalized_entity["attributes"] = normalized_attributes
        normalized_entities.append(normalized_entity)
        changed = True
    if not changed:
        return payload
    result = dict(payload)
    result["entities"] = normalized_entities
    return result


def _normalize_incomplete_case_target_to_clarification(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed when the model emits a blank Case reference.

    A Case-progress statement without a Case name is a normal conversational
    situation, not a malformed-runtime failure. Models sometimes represent it
    as ``case_ref.value == ""`` while still binding a write action. Remove only
    that ungrounded Case action and turn it into an explicit Case question. Any
    unrelated, valid actions in a multi-intent turn remain untouched.
    """

    raw_entities = payload.get("entities")
    raw_actions = payload.get("required_actions")
    if not isinstance(raw_entities, list) or not isinstance(raw_actions, list):
        return payload

    invalid_entity_ids: set[str] = set()
    has_blank_case_ref = False
    normalized_entities: list[Any] = []
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            normalized_entities.append(raw_entity)
            continue
        entity_type = str(raw_entity.get("entity_type") or "").strip()
        entity_id = str(raw_entity.get("entity_id") or "").strip()
        value = str(raw_entity.get("value") or "").strip()
        if entity_type == "case_ref" and (not entity_id or not value):
            has_blank_case_ref = True
            if entity_id:
                invalid_entity_ids.add(entity_id)
            continue
        normalized_entities.append(raw_entity)
    if not has_blank_case_ref:
        return payload

    valid_entity_ids = {
        str(item.get("entity_id") or "").strip()
        for item in normalized_entities
        if isinstance(item, dict) and str(item.get("entity_id") or "").strip()
    }

    case_action_types = {
        "record_case_progress",
        "update_case_progress",
        "delete_case_progress",
        "query_case_progress",
        "link_case_progress",
    }
    removed_action_ids: set[str] = set()
    normalized_actions: list[Any] = []
    for raw_action in raw_actions:
        if not isinstance(raw_action, dict):
            normalized_actions.append(raw_action)
            continue
        action_type = str(raw_action.get("action_type") or "").strip()
        entity_ids = {
            str(value or "").strip()
            for value in raw_action.get("entity_ids", [])
        } if isinstance(raw_action.get("entity_ids"), list) else set()
        if action_type in case_action_types and (
            not entity_ids
            or "" in entity_ids
            or bool(entity_ids.intersection(invalid_entity_ids))
            or not entity_ids.issubset(valid_entity_ids)
        ):
            action_id = str(raw_action.get("action_id") or "").strip()
            if action_id:
                removed_action_ids.add(action_id)
            continue
        normalized_actions.append(raw_action)

    normalized_segments: list[Any] = []
    for raw_segment in payload.get("segments", []):
        if not isinstance(raw_segment, dict):
            normalized_segments.append(raw_segment)
            continue
        segment = dict(raw_segment)
        if isinstance(segment.get("entity_ids"), list):
            segment["entity_ids"] = [
                value
                for value in segment["entity_ids"]
                if str(value or "").strip()
                and str(value).strip() not in invalid_entity_ids
            ]
        if isinstance(segment.get("action_ids"), list):
            segment["action_ids"] = [
                value
                for value in segment["action_ids"]
                if str(value or "").strip() not in removed_action_ids
            ]
        normalized_segments.append(segment)

    context_update = payload.get("context_update")
    normalized_context = dict(context_update) if isinstance(context_update, dict) else {}
    if isinstance(normalized_context.get("remember_entity_ids"), list):
        normalized_context["remember_entity_ids"] = [
            value
            for value in normalized_context["remember_entity_ids"]
            if str(value or "").strip()
            and str(value).strip() not in invalid_entity_ids
        ]
    pending = normalized_context.get("bind_pending")
    if isinstance(pending, dict):
        pending_entity_ids = {
            str(value or "").strip()
            for value in pending.get("entity_ids", [])
        } if isinstance(pending.get("entity_ids"), list) else set()
        if (
            not pending_entity_ids
            or "" in pending_entity_ids
            or bool(pending_entity_ids.intersection(invalid_entity_ids))
        ):
            normalized_context.pop("bind_pending", None)

    result = dict(payload)
    result["entities"] = normalized_entities
    result["required_actions"] = normalized_actions
    result["segments"] = normalized_segments
    result["context_update"] = normalized_context
    clarification = result.get("clarification_need")
    if (
        not clarification
        or (
            isinstance(clarification, dict)
            and clarification.get("reason") == "ambiguous_case_alias"
            and not valid_entity_ids
        )
    ):
        result["clarification_need"] = {
            "reason": "case_target_required",
            "missing_fields": ["case_reference"],
            "question": "请告诉我具体是哪个案件，可以发案件编号、简称或完整名称。",
        }
    return result


def _normalize_overlapping_case_mutations(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Prevent one edit/delete utterance from also creating a new progress row.

    The model may describe replacement text as both ``update_case_progress`` and
    ``record_case_progress``. When those actions bind the same source segment,
    overlapping spans, or the same source text, executing both would duplicate
    the user's fact. Keep the explicit edit/delete and remove only the overlapping
    create action. Distinct, non-overlapping multi-Case segments remain intact.
    """

    raw_actions = payload.get("required_actions")
    raw_segments = payload.get("segments")
    if not isinstance(raw_actions, list) or not isinstance(raw_segments, list):
        return payload
    mutation_ids = {
        str(action.get("action_id") or "").strip()
        for action in raw_actions
        if isinstance(action, dict)
        and action.get("action_type") in {"update_case_progress", "delete_case_progress"}
        and str(action.get("action_id") or "").strip()
    }
    record_ids = {
        str(action.get("action_id") or "").strip()
        for action in raw_actions
        if isinstance(action, dict)
        and action.get("action_type") == "record_case_progress"
        and str(action.get("action_id") or "").strip()
    }
    if not mutation_ids or not record_ids:
        return payload

    segments_by_action: dict[str, list[dict[str, Any]]] = {}
    for segment in raw_segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("action_ids"), list):
            continue
        for action_id in segment["action_ids"]:
            normalized_id = str(action_id or "").strip()
            if normalized_id:
                segments_by_action.setdefault(normalized_id, []).append(segment)

    def overlaps(first: dict[str, Any], second: dict[str, Any]) -> bool:
        if str(first.get("segment_id") or "") == str(second.get("segment_id") or ""):
            return True
        first_text = "".join(str(first.get("text") or "").split())
        second_text = "".join(str(second.get("text") or "").split())
        if first_text and second_text and (
            first_text == second_text
            or first_text in second_text
            or second_text in first_text
        ):
            return True
        try:
            first_start = int(first.get("start_offset", -1))
            first_end = int(first.get("end_offset", -1))
            second_start = int(second.get("start_offset", -1))
            second_end = int(second.get("end_offset", -1))
        except (TypeError, ValueError):
            return False
        return (
            first_start >= 0
            and second_start >= 0
            and first_end > first_start
            and second_end > second_start
            and max(first_start, second_start) < min(first_end, second_end)
        )

    removed_record_ids: set[str] = set()
    for record_id in record_ids:
        record_segments = segments_by_action.get(record_id, [])
        for mutation_id in mutation_ids:
            mutation_segments = segments_by_action.get(mutation_id, [])
            if not record_segments or not mutation_segments or any(
                overlaps(record_segment, mutation_segment)
                for record_segment in record_segments
                for mutation_segment in mutation_segments
            ):
                removed_record_ids.add(record_id)
                break
    if not removed_record_ids:
        return payload

    result = dict(payload)
    result["required_actions"] = [
        action
        for action in raw_actions
        if not isinstance(action, dict)
        or str(action.get("action_id") or "").strip() not in removed_record_ids
    ]
    normalized_segments: list[Any] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            normalized_segments.append(raw_segment)
            continue
        segment = dict(raw_segment)
        if isinstance(segment.get("action_ids"), list):
            segment["action_ids"] = [
                action_id
                for action_id in segment["action_ids"]
                if str(action_id or "").strip() not in removed_record_ids
            ]
        normalized_segments.append(segment)
    result["segments"] = normalized_segments
    return result


def _normalize_new_case_progress_case_hint(
    payload: dict[str, Any],
    *,
    turn: CognitiveTurn,
) -> dict[str, Any]:
    """Migrate one bounded model alias without widening the closed contract.

    ``case_hint`` belongs to references for existing progress records. Some model
    responses duplicate the already-grounded Case reference into that attribute
    for a new ``record_case_progress`` action. We may remove it only when the
    action binding, the entity value, and the uniquely authorized visible Case all
    agree. Conflicts remain untouched and are rejected by the contract validator.
    """

    raw_entities = payload.get("entities")
    raw_actions = payload.get("required_actions")
    if not isinstance(raw_entities, list) or not isinstance(raw_actions, list):
        return payload
    progress_entity_ids = {
        str(entity_id)
        for action in raw_actions
        if isinstance(action, dict)
        and action.get("action_type") == "record_case_progress"
        and isinstance(action.get("entity_ids"), list)
        for entity_id in action["entity_ids"]
    }
    if not progress_entity_ids:
        return payload
    authorized_reference = _unique_authorized_case_reference(
        turn.text,
        turn.resources,
    )
    if not authorized_reference:
        return payload
    authorized_key = _normalize_case_reference(authorized_reference)

    normalized_entities: list[Any] = []
    changed = False
    for raw_entity in raw_entities:
        if (
            not isinstance(raw_entity, dict)
            or raw_entity.get("entity_type") != "case_ref"
            or str(raw_entity.get("entity_id") or "") not in progress_entity_ids
        ):
            normalized_entities.append(raw_entity)
            continue
        attributes = raw_entity.get("attributes")
        if not isinstance(attributes, dict) or "case_hint" not in attributes:
            normalized_entities.append(raw_entity)
            continue
        case_hint = attributes.get("case_hint")
        entity_value = raw_entity.get("value")
        if (
            not isinstance(case_hint, str)
            or not isinstance(entity_value, str)
            or _normalize_case_reference(case_hint) != authorized_key
            or _normalize_case_reference(entity_value) != authorized_key
        ):
            normalized_entities.append(raw_entity)
            continue
        normalized_entity = dict(raw_entity)
        normalized_attributes = dict(attributes)
        normalized_attributes.pop("case_hint", None)
        normalized_entity["attributes"] = normalized_attributes
        normalized_entities.append(normalized_entity)
        changed = True
    if not changed:
        return payload
    result = dict(payload)
    result["entities"] = normalized_entities
    return result


def _normalize_asserted_case_progress_statement_mode(
    payload: dict[str, Any],
    *,
    turn: CognitiveTurn,
) -> dict[str, Any]:
    """Fill one missing assertion marker from grounded action/segment evidence."""

    raw_entities = payload.get("entities")
    raw_actions = payload.get("required_actions")
    raw_segments = payload.get("segments")
    if not all(
        isinstance(value, list)
        for value in (raw_entities, raw_actions, raw_segments)
    ):
        return payload
    authorized_reference = _unique_authorized_case_reference(
        turn.text,
        turn.resources,
    )
    if not authorized_reference:
        return payload
    authorized_key = _normalize_case_reference(authorized_reference)
    action_by_entity: dict[str, str] = {}
    for action in raw_actions:
        if (
            not isinstance(action, dict)
            or action.get("action_type") != "record_case_progress"
            or not isinstance(action.get("entity_ids"), list)
            or len(action["entity_ids"]) != 1
        ):
            continue
        action_by_entity[str(action["entity_ids"][0])] = str(
            action.get("action_id") or ""
        )
    if not action_by_entity:
        return payload

    segment_by_action: dict[str, str] = {}
    duplicate_action_ids: set[str] = set()
    for segment in raw_segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("action_ids"), list):
            continue
        segment_text = str(segment.get("text") or "")
        for action_id_value in segment["action_ids"]:
            action_id = str(action_id_value)
            if action_id in segment_by_action:
                duplicate_action_ids.add(action_id)
            else:
                segment_by_action[action_id] = segment_text

    normalized_entities: list[Any] = []
    changed = False
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            normalized_entities.append(raw_entity)
            continue
        entity_id = str(raw_entity.get("entity_id") or "")
        action_id = action_by_entity.get(entity_id, "")
        attributes = raw_entity.get("attributes")
        segment_text = segment_by_action.get(action_id, "")
        if (
            raw_entity.get("entity_type") != "case_ref"
            or not action_id
            or action_id in duplicate_action_ids
            or not isinstance(attributes, dict)
            or "statement_mode" in attributes
            or _normalize_case_reference(raw_entity.get("value")) != authorized_key
            or segment_text not in turn.text
            or not assess_case_progress_statement(segment_text).asserted
        ):
            normalized_entities.append(raw_entity)
            continue
        normalized_entity = dict(raw_entity)
        normalized_attributes = dict(attributes)
        normalized_attributes["statement_mode"] = "asserted"
        normalized_entity["attributes"] = normalized_attributes
        normalized_entities.append(normalized_entity)
        changed = True
    if not changed:
        return payload
    result = dict(payload)
    result["entities"] = normalized_entities
    return result


def _enforce_explicit_travel_event(
    payload: dict[str, Any],
    *,
    text: str,
) -> dict[str, Any]:
    """Keep a closed-form trip assertion as a primary domain action."""

    extracted = _extract_explicit_travel_event(text)
    if extracted is None:
        return payload
    existing_actions = [
        item
        for item in payload.get("required_actions", [])
        if isinstance(item, dict)
    ]
    if any(
        str(item.get("action_type") or "") == "record_travel_event"
        for item in existing_actions
    ):
        return payload

    result = dict(payload)
    intents = [str(value) for value in payload.get("intents", [])]
    if "travel_event" not in intents:
        intents.append("travel_event")
    result["intents"] = intents

    entity_id = "contract-explicit-travel-event"
    action_id = "contract-record-explicit-travel"
    entities = [
        dict(item)
        for item in payload.get("entities", [])
        if isinstance(item, dict)
    ]
    if entity_id in {str(item.get("entity_id") or "") for item in entities}:
        entity_id = f"{entity_id}-supplement"
    if action_id in {
        str(item.get("action_id") or "") for item in existing_actions
    }:
        action_id = f"{action_id}-supplement"
    entities.append(
        {
            "entity_id": entity_id,
            "entity_type": "travel_event",
            "value": str(text or "").strip(),
            "confidence": 1.0,
            "attributes": extracted,
        }
    )
    result["entities"] = entities
    result["required_actions"] = [
        *existing_actions,
        {
            "action_id": action_id,
            "action_type": "record_travel_event",
            "intent": "travel_event",
            "entity_ids": [entity_id],
            "parameters": {},
        },
    ]

    segments = [
        dict(item)
        for item in payload.get("segments", [])
        if isinstance(item, dict)
    ]
    containing = next(
        (
            item
            for item in segments
            if str(text or "").strip() == str(item.get("text") or "").strip()
        ),
        None,
    )
    if containing is None:
        return payload
    containing["intents"] = list(
        dict.fromkeys([*containing.get("intents", []), "travel_event"])
    )
    containing["entity_ids"] = list(
        dict.fromkeys([*containing.get("entity_ids", []), entity_id])
    )
    containing["action_ids"] = list(
        dict.fromkeys([*containing.get("action_ids", []), action_id])
    )
    result["segments"] = segments

    context_update = dict(payload.get("context_update") or {})
    context_update["remember_entity_ids"] = list(
        dict.fromkeys([*context_update.get("remember_entity_ids", []), entity_id])
    )
    context_update["remember_turn"] = True
    result["context_update"] = context_update
    return result


def _enforce_operation_status_query(
    payload: dict[str, Any],
    *,
    text: str,
) -> dict[str, Any]:
    compact = re.sub(r"[\s，。！？、,.!?]", "", str(text or ""))
    case_query = re.fullmatch(
        r"(?:刚才(?:那条)?|上一条|这条)?"
        r"(?:有没有|有没|有|已经|是否)?"
        r"(?:进入|写入|写到|记入|记到|记录到|记录成)?"
        r"案件进展"
        r"(?:里|中)?"
        r"(?:了吗|没有|没|成功了吗|成功没有)?",
        compact,
    )
    travel_query = re.fullmatch(
        r"(?:现在|目前)?"
        r"(?:有没有|有无|有)"
        r"(?:人|同事)?"
        r"(?:和我|跟我)?"
        r"(?:出差)?"
        r"(?:协同|同行)"
        r"(?:了|吗)?",
        compact,
    ) or re.fullmatch(
        r"(?:现在|目前)?(?:出差)?(?:协同|同行)(?:现在|目前)?(?:是什么|什么|当前)?状态(?:了|吗)?",
        compact,
    )
    if case_query is None and travel_query is None:
        return payload
    domain = "case_progress" if case_query is not None else "travel"
    intent = (
        "case_progress_query"
        if domain == "case_progress"
        else "travel_collaboration_query"
    )
    entity_id = f"contract-{domain}-operation-status"
    action_id = f"contract-query-{domain}-operation-status"
    return {
        "intents": [intent],
        "segments": [
            {
                "segment_id": f"contract-{domain}-operation-status-segment",
                "text": str(text or "").strip(),
                "intents": [intent],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
            }
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "entity_type": "operation_status_query",
                "value": str(text or "").strip(),
                "confidence": 1.0,
                "attributes": {"domain": domain},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": action_id,
                "action_type": "query_operation_status",
                "intent": intent,
                "entity_ids": [entity_id],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {"preserve_current_goal": True, "remember_turn": True},
    }


def _extract_explicit_travel_event(text: str) -> dict[str, str] | None:
    compact = re.sub(r"[\s，。！？、,.!?]", "", str(text or ""))
    match = re.fullmatch(
        r"(?:我)?"
        r"(?P<date>明天|明日|后天|下周一|\d{1,2}号)"
        r"(?:上午|中午|下午|晚上)?"
        r"(?:计划|准备|打算|预计|要|将|会)?"
        r"(?:"
        r"(?:去|前往|到)(?P<destination_before>[\u4e00-\u9fff]{2,8}?)(?:市)?出差"
        r"|出差(?:去|前往|到)?(?P<destination_after>[\u4e00-\u9fff]{2,8}?)(?:市)?"
        r")",
        compact,
    )
    if match is None:
        return None
    destination = str(
        match.group("destination_before")
        or match.group("destination_after")
        or ""
    ).strip()
    if len(destination) < 2:
        return None
    date_hint = {
        "明天": "tomorrow",
        "明日": "tomorrow",
        "后天": "day_after_tomorrow",
        "下周一": "next_monday",
    }.get(match.group("date"), match.group("date"))
    return {
        "destination": destination,
        "date_hint": date_hint,
        "purpose": "出差",
    }


def _enforce_authorized_case_progress(
    payload: dict[str, Any],
    *,
    text: str,
    resources: dict[str, Any],
) -> dict[str, Any]:
    """Restore an explicit progress action only from one authorized case ref."""

    actions = [
        item
        for item in payload.get("required_actions", [])
        if isinstance(item, dict)
    ]
    if any(str(item.get("action_type") or "") == "record_case_progress" for item in actions):
        return payload
    if not _is_explicit_case_progress_assertion(text):
        return payload
    case_reference = _unique_authorized_case_reference(text, resources)
    if not case_reference:
        return payload

    segments = [
        dict(item)
        for item in payload.get("segments", [])
        if isinstance(item, dict)
    ]
    containing = next(
        (
            item
            for item in segments
            if str(text or "").strip() == str(item.get("text") or "").strip()
        ),
        None,
    )
    if containing is None:
        return payload

    result = dict(payload)
    intents = [str(value) for value in payload.get("intents", [])]
    if "case_progress" not in intents:
        intents.append("case_progress")
    result["intents"] = intents

    entity_id = "contract-authorized-case-progress"
    action_id = "contract-record-authorized-case-progress"
    entities = [
        dict(item)
        for item in payload.get("entities", [])
        if isinstance(item, dict)
    ]
    if entity_id in {str(item.get("entity_id") or "") for item in entities}:
        entity_id = f"{entity_id}-supplement"
    if action_id in {str(item.get("action_id") or "") for item in actions}:
        action_id = f"{action_id}-supplement"
    entities.append(
        {
            "entity_id": entity_id,
            "entity_type": "case_ref",
            "value": case_reference,
            "confidence": 1.0,
            "attributes": {},
        }
    )
    result["entities"] = entities
    result["required_actions"] = [
        *actions,
        {
            "action_id": action_id,
            "action_type": "record_case_progress",
            "intent": "case_progress",
            "entity_ids": [entity_id],
            "parameters": {},
        },
    ]
    containing["intents"] = list(
        dict.fromkeys([*containing.get("intents", []), "case_progress"])
    )
    containing["entity_ids"] = list(
        dict.fromkeys([*containing.get("entity_ids", []), entity_id])
    )
    containing["action_ids"] = list(
        dict.fromkeys([*containing.get("action_ids", []), action_id])
    )
    result["segments"] = segments

    context_update = dict(payload.get("context_update") or {})
    context_update["current_goal"] = "case_progress"
    context_update.pop("preserve_current_goal", None)
    context_update["remember_entity_ids"] = list(
        dict.fromkeys([*context_update.get("remember_entity_ids", []), entity_id])
    )
    context_update["remember_turn"] = True
    result["context_update"] = context_update
    return result


def _is_explicit_case_progress_assertion(text: str) -> bool:
    return assess_case_progress_statement(text).asserted


def _unique_authorized_case_reference(
    text: str,
    resources: dict[str, Any],
) -> str:
    matches = _authorized_case_reference_matches(text, resources)
    if len(matches) != 1:
        return ""
    return str(matches[0]["reference"])


def _authorized_case_reference_matches(
    text: str,
    resources: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    visible_cases = resources.get("visible_cases")
    if not isinstance(visible_cases, list):
        return ()
    return tuple(
        {
            "case_id": str(match.case.get("case_id") or ""),
            "case_number": str(match.case.get("case_number") or ""),
            "case_name": str(match.case.get("case_name") or ""),
            "reference": match.reference,
            "version": int(match.case.get("version") or 0),
            "trusted_references": _trusted_case_references(
                match.reference,
                match.case,
            ),
        }
        for match in discover_visible_case_references(text, visible_cases)
    )


def _normalize_case_reference(value: object) -> str:
    return normalize_case_reference(value)


def _trusted_case_references(
    matched_reference: str,
    case: Mapping[str, Any],
) -> tuple[str, ...]:
    aliases = case.get("confirmed_aliases")
    aliases = aliases if isinstance(aliases, (list, tuple)) else ()
    return tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (
                matched_reference,
                case.get("case_id"),
                case.get("case_number"),
                case.get("external_case_id"),
                case.get("case_name"),
                *aliases,
            )
            if str(value or "").strip()
        )
    )


def _enforce_daily_deictic_projection(
    payload: dict[str, Any],
    *,
    text: str,
    resources: dict[str, Any],
    state: ConversationState,
    occurred_at: datetime,
) -> dict[str, Any]:
    """Resolve a plural future-plan reference only from one active daily snapshot.

    The language model may classify “明天继续做这两件事情” as a literal plan.
    Persisting that pronoun destroys the reference. This protocol either binds
    the reference to the complete, uniquely-sized current-work set or produces
    a clarification with zero actions.
    """

    expected_count = _daily_deictic_projection_count(text)
    if expected_count is None:
        return payload
    draft = resources.get("daily_draft")
    draft = draft if isinstance(draft, dict) else {}
    policy = resources.get("daily_policy")
    policy = policy if isinstance(policy, dict) else {}
    items = draft.get("items")
    items = items if isinstance(items, list) else []
    current_work = [
        item for item in items
        if isinstance(item, dict)
        and str(item.get("field") or "") == "today_work"
        and str(item.get("text") or "").strip()
    ]
    resolved_count = len(current_work) if expected_count == -1 else expected_count
    has_unique_context = bool(
        str(draft.get("report_id") or "").strip()
        and str(draft.get("status") or "") == "collecting"
        and resolved_count > 0
        and len(current_work) == resolved_count
        and not state.active_pending(occurred_at)
    )
    if not has_unique_context:
        actual_count = len(current_work)
        return {
            "intents": ["daily_modify"],
            "segments": [
                {
                    "segment_id": "daily-deictic-reference-clarification",
                    "text": text,
                    "intents": ["daily_modify"],
                    "entity_ids": [],
                    "action_ids": [],
                }
            ],
            "entities": [],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": {
                "reason": "daily_deictic_reference_not_unique",
                "missing_fields": ["target_item_ids"],
                "question": (
                    f"当前日报有 {actual_count} 项今日工作，请明确你指的是哪 {resolved_count} 项。"
                    if actual_count
                    else "当前日报里没有可唯一对应的今日工作，请直接说出明天要继续的事项。"
                ),
            },
            "context_update": {
                "preserve_current_goal": True,
                "remember_turn": True,
            },
        }
    report_entity_id = "daily-deictic-current-report"
    action_id = "daily-deictic-copy-current-work"
    return {
        "intents": ["daily_modify"],
        "segments": [
            {
                "segment_id": "daily-deictic-reference-resolved",
                "text": text,
                "intents": ["daily_modify"],
                "entity_ids": [report_entity_id],
                "action_ids": [action_id],
            }
        ],
        "entities": [
            {
                "entity_id": report_entity_id,
                "entity_type": "daily_report",
                "value": "当前日报",
                "confidence": 1.0,
                "attributes": {
                    "report_id": str(draft.get("report_id") or ""),
                    "version": int(draft.get("version") or 0),
                    "report_date": str(policy.get("current_report_date") or ""),
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": action_id,
                "action_type": "copy_current_work_to_tomorrow",
                "intent": "daily_modify",
                "entity_ids": [report_entity_id],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "preserve_current_goal": True,
            "remember_entity_ids": [report_entity_id],
            "remember_turn": True,
        },
    }


def _daily_deictic_projection_count(text: str) -> int | None:
    compact = re.sub(r"[\s，。！？、,.!?]", "", str(text or ""))
    if not compact.startswith(("明天", "明日")) or len(compact) > 24:
        return None
    remainder = compact[2:]
    reference = re.search(
        r"(?:这|那)(?:(?P<count>一|二|两|俩|三|四|五|六|七|八|九|十)(?:件|项|个)|(?P<all>些))(?:事情|事|工作|任务)",
        remainder,
    )
    if reference is None:
        return None
    continuation = remainder[: reference.start()] + remainder[reference.end() :]
    if not re.fullmatch(r"(?:继续|接着|还要|仍然)(?:做|干|推进|处理)?(?:吧|了)?", continuation):
        return None
    if reference.group("all"):
        # The caller resolves “这些” to the complete current-work set; use -1
        # here and normalize it after reading the trusted snapshot.
        return -1
    return {
        "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }.get(str(reference.group("count") or ""))


def _enforce_report_meta_opening(payload: dict[str, Any], *, text: str) -> dict[str, Any]:
    """Enter the right periodic Report context without persisting the opener."""

    report_type = report_type_from_meta_opening(text)
    if report_type is None:
        return payload
    intent = f"{report_type}_report"
    return {
        "intents": [intent],
        "segments": [
            {
                "segment_id": f"{report_type}-report-meta-opening",
                "text": text,
                "intents": [intent],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {
            "current_goal": intent,
            "remember_turn": True,
        },
    }


def _report_meta_opening_semantic_payload(text: str) -> dict[str, Any] | None:
    """Establish an explicit report goal before a competing model intent."""

    if report_type_from_meta_opening(text) is None:
        return None
    return _enforce_report_meta_opening({}, text=text)


def is_daily_report_meta_opening(text: str) -> bool:
    return report_type_from_meta_opening(text) == "daily"


def report_type_from_meta_opening(text: str) -> str | None:
    compact = re.sub(r"[\s，。！？、,.!?]", "", str(text or ""))
    if not compact or len(compact) > 18:
        return None
    for report_type, label in (
        ("daily", "日报"),
        ("weekly", "周报"),
        ("monthly", "月报"),
    ):
        if label not in compact:
            continue
        if re.fullmatch(
            rf"(?:我)?(?:"
            rf"(?:(?:要|想|准备|现在)?(?:填|写|开始填|开始写)"
            rf"(?:今天|今日|本周|这周|本月|这个月)?(?:的)?(?:个)?)"
            rf"|(?:进入|打开|切到|切换到|开始)"
            rf")?{label}(?:了|吧|呢|哈)?",
            compact,
        ):
            return report_type
    return None


def _active_daily_plain_content_semantic_payload(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
) -> dict[str, Any] | None:
    """Default a plain work statement to today only in trusted Daily context."""

    if not _active_daily_collection_context(turn=turn, state=state):
        return None
    source = str(turn.text or "").strip()
    if not _plain_daily_work_statement(source, resources=turn.resources):
        return None
    entity_id = "active-daily-plain-work"
    action_id = "active-daily-capture-work"
    return {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "active-daily-plain-work-segment",
                "text": source,
                "intents": ["daily_append"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": 0,
                "end_offset": len(source),
            }
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "entity_type": "daily_event",
                "value": source,
                "confidence": 1.0,
                "attributes": {"field": "today_work"},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": action_id,
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": [entity_id],
            "remember_turn": True,
        },
    }


def _active_daily_collection_context(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
) -> bool:
    goal = str(getattr(state.current_goal, "intent", "") or "")
    if goal in {"daily_report", "daily_append", "daily_modify"}:
        return True
    raw_tasks = turn.resources.get("active_tasks")
    if not isinstance(raw_tasks, (list, tuple)):
        return False
    for raw_task in raw_tasks:
        if not isinstance(raw_task, Mapping):
            continue
        task_kind = " ".join(
            str(raw_task.get(key) or "").strip().lower()
            for key in ("workflow", "task_type", "domain", "intent", "type")
        )
        status = str(raw_task.get("status") or "active").strip().lower()
        if (
            any(value in task_kind for value in ("daily_report", "daily", "日报"))
            and status in {"active", "collecting", "in_progress", "pending"}
        ):
            return True
    return False


def _plain_daily_work_statement(
    text: str,
    *,
    resources: Mapping[str, Any],
) -> bool:
    source = str(text or "").strip()
    if len(source) < 2 or len(source) > 120 or "\n" in source:
        return False
    if report_type_from_meta_opening(source) is not None:
        return False
    if re.search(r"[?？]", source) or re.search(
        r"(?:什么|为何|为什么|怎么|如何|哪里|哪个|谁|是否|能否|可否|请问|流程|时间安排)",
        source,
    ):
        return False
    if re.search(
        r"(?:明天|明日|后天|下周|下月)"
        r"|(?:没|没有|无)(?:其他|其它)?(?:问题|风险)",
        source,
    ):
        return False
    compact = re.sub(r"[\s，。！？、,.!?]", "", source)
    if len(compact) < 2 or compact in {
        "好的",
        "好吧",
        "可以",
        "确认",
        "取消",
        "算了",
        "不用了",
        "谢谢",
    }:
        return False
    if re.search(
        r"(?:关闭|结案|撤销|查询|查看|查一下|更新).{0,12}(?:案件|案子|案号)"
        r"|(?:案件|案子|案号).{0,12}(?:关闭|结案|撤销|查询|查看|进展)"
        r"|(?:取消|修改|改期|新增|安排).{0,12}(?:出差|差旅)"
        r"|(?:出差|差旅).{0,12}(?:取消|修改|改期|新增|安排)",
        source,
    ):
        return False
    if re.search(
        r"(?:案件|案子|案号|法院|开庭|庭审|调解|判决|裁定|执行)"
        r"|(?:出差|差旅|行程)"
        r"|(?:周报|月报)",
        source,
    ):
        return False
    visible_cases = resources.get("visible_cases")
    if isinstance(visible_cases, (list, tuple)):
        for raw_case in visible_cases:
            if not isinstance(raw_case, Mapping):
                continue
            labels = [
                str(raw_case.get(key) or "").strip()
                for key in ("case_name", "case_number", "external_case_id")
            ]
            aliases = raw_case.get("confirmed_aliases")
            if isinstance(aliases, (list, tuple)):
                labels.extend(str(value or "").strip() for value in aliases)
            if any(label and label in source for label in labels):
                return False
    return True


def _enforce_report_task_exit(
    payload: dict[str, Any],
    *,
    text: str,
    state: ConversationState,
) -> dict[str, Any]:
    report_type = report_type_from_task_exit(text)
    active_intent = str(getattr(state.current_goal, "intent", "") or "")
    if report_type is None or active_intent != f"{report_type}_report":
        return payload
    exit_intent = f"{report_type}_report_exit"
    return {
        "intents": [exit_intent],
        "segments": [
            {
                "segment_id": f"{report_type}-report-task-exit",
                "text": text,
                "intents": [exit_intent],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {
            "clear_current_goal": True,
            "remember_turn": True,
        },
    }


def report_type_from_task_exit(text: str) -> str | None:
    compact = re.sub(r"[\s，。！？、,.!?]", "", str(text or ""))
    if not compact or len(compact) > 18:
        return None
    for report_type, label in (
        ("daily", "日报"),
        ("weekly", "周报"),
        ("monthly", "月报"),
    ):
        if re.fullmatch(
            rf"(?:这个|当前)?{label}(?:任务|填写)?(?:结束了?|先结束|不写了|先到这|到此结束)",
            compact,
        ):
            return report_type
    return None


def _enforce_cross_domain_context_fencing(
    payload: dict[str, Any],
    *,
    text: str,
    state: ConversationState,
) -> dict[str, Any]:
    """Prevent a stale Case reference from authorizing a later cross-domain write."""

    mutation_actions = {
        "record_case_progress",
        "update_case_progress",
        "delete_case_progress",
        "link_case_progress",
    }
    actions = [item for item in payload.get("required_actions", []) if isinstance(item, dict)]
    if not any(str(item.get("action_type") or "") in mutation_actions for item in actions):
        return payload
    current_intent = str(getattr(state.current_goal, "intent", "") or "")
    case_focus_active = current_intent.startswith("case_") or current_intent == "case_progress"
    explicit_case_mutation = bool(
        re.search(
            r"(?:案件|案子|案号|.{1,20}案).{0,24}(?:进展|开庭|庭审|执行|查控|立案|判决|裁定|结案|调解|法院)"
            r"|(?:联系法院|推进案件|更新案件|记录案件|案件进展)",
            str(text or ""),
        )
    )
    entities = [item for item in payload.get("entities", []) if isinstance(item, dict)]
    depends_on_old_context = any(
        isinstance(item.get("attributes"), dict)
        and isinstance(item["attributes"].get("context_reference"), dict)
        for item in entities
    )
    if case_focus_active or explicit_case_mutation:
        return payload
    if not current_intent and not depends_on_old_context:
        return payload
    return {
        "intents": ["chat"],
        "segments": [
            {
                "segment_id": "cross-domain-stale-case-reference",
                "text": text,
                "intents": ["chat"],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"preserve_current_goal": True, "remember_turn": True},
    }


def _enforce_non_unique_short_confirmation(
    payload: dict[str, Any],
    *,
    text: str,
    state: ConversationState,
    occurred_at: datetime,
) -> dict[str, Any]:
    """Fail safely when a bare acknowledgement cannot bind one pending item."""

    if not _is_short_confirmation(text) or len(state.active_pending(occurred_at)) == 1:
        return payload
    # Do not preserve model-proposed entities/actions here: a bare acknowledgement
    # cannot establish a legal business object when the state has no unique binding.
    return {
        "intents": ["clarify"],
        "segments": [
            {
                "segment_id": "non-unique-confirmation",
                "text": text,
                "intents": ["clarify"],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": {
            "reason": "pending_binding_mismatch",
            "missing_fields": ["pending_binding"],
            "question": "这条确认没有匹配到唯一的待处理事项，请说明要继续哪一项。",
        },
        "context_update": {"preserve_current_goal": True},
    }


def _is_short_confirmation(text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?]", "", text)
    for filler in ("那个", "就这样哈", "就这样", "哈", "嗯", "哦"):
        compact = compact.replace(filler, "")
    return compact in {"确认", "是", "是的", "对", "对的", "确定", "没错"}


def _state_payload(state: ConversationState) -> dict[str, Any]:
    return {
        "user_id": state.user_id,
        "conversation_id": state.conversation_id,
        "version": state.version,
        "current_goal": asdict(state.current_goal) if state.current_goal is not None else None,
        "goal_stack": [asdict(goal) for goal in state.goal_stack],
        "current_entities": [asdict(entity) for entity in state.current_entities[-20:]],
        "recent_context": [
            {
                **asdict(frame),
                "occurred_at": frame.occurred_at.isoformat(),
            }
            for frame in state.recent_context[-12:]
        ],
        "pending": [
            {
                **asdict(item),
                "created_at": item.created_at.isoformat(),
                "expires_at": item.expires_at.isoformat(),
            }
            for item in state.pending
        ],
        "user_constraints": asdict(state.user_constraints),
    }


def _conversation_state_oracle_guard_payload(state: ConversationState) -> dict[str, Any]:
    """Expose trusted optimistic versions without weakening oracle checks elsewhere."""

    payload = state.as_payload()
    sanitized_entities = []
    for raw in payload.get("current_entities", []):
        item = dict(raw)
        attributes = dict(item.get("attributes") or {})
        if item.get("entity_type") == "case_progress_ref":
            attributes.pop("expected_version", None)
        item["attributes"] = attributes
        sanitized_entities.append(item)
    payload["current_entities"] = sanitized_entities
    return payload


def _validate_turn_contract(
    turn: CognitiveTurn,
    state: ConversationState,
    interpretation: SemanticInterpretation,
) -> None:
    cursor = 0
    for segment in interpretation.segments:
        location = turn.text.find(segment.text, cursor)
        if location < 0:
            raise ValueError("semantic segments must be ordered exact substrings of the turn")
        cursor = location + len(segment.text)
    draft = turn.resources.get("daily_draft")
    draft = draft if isinstance(draft, dict) else {}
    constraints = state.user_constraints
    submit_is_available = (
        str(draft.get("report_id") or "").strip()
        and str(draft.get("status") or "") == "collecting"
        and not constraints.read_only
        and not constraints.no_daily_write
        and not constraints.draft_only
    )
    if "daily_submit" not in interpretation.intents or not submit_is_available:
        return
    submit_actions = [
        action
        for action in interpretation.required_actions
        if action.action_type == "submit_daily_report" and action.intent == "daily_submit"
    ]
    pending = interpretation.context_update.bind_pending
    if len(submit_actions) != 1:
        raise ValueError("daily_submit for the active collecting report requires submit_daily_report action")
    if pending is not None and pending.intent == "daily_submit":
        raise ValueError("normal daily_submit must not create confirmation pending")
    if (
        interpretation.clarification_need is not None
        and interpretation.clarification_need.reason == "high_impact_confirmation_required"
    ):
        raise ValueError("normal daily_submit is low risk and must not require confirmation")
