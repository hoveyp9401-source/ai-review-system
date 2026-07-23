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
    SemanticSegment,
    is_explicit_pending_cancellation,
    is_explicit_pending_confirmation,
)
from app.agent2.conversation_state import ConversationEntity, ConversationState
from app.agent2.cognitive_contract_v3 import (
    ACTION_PARAMETER_KEYS,
    validate_semantic_interpretation_contract,
)
from app.agent2.domain_admission import evaluate_assertion_polarity_contract
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
    explicit_standalone_daily_fact_semantic_payload,
    parse_structured_daily_document,
    structured_daily_semantic_payload,
)
from app.utils.json import extract_json_object


PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "cognitive_core_v3.md"
MAX_COGNITIVE_INPUT_CHARS = 2000
_MODEL_SEMANTIC_TOP_LEVEL_FIELDS = frozenset(
    {
        "intents",
        "segments",
        "entities",
        "confidence",
        "required_actions",
        "clarification_need",
        "context_update",
    }
)


def _validate_model_semantic_payload_shape(payload: Mapping[str, Any]) -> None:
    """Reject fragments or wrappers before defaults can turn them into cognition.

    The model contract requires one object with exactly seven top-level fields.
    In particular, a response containing several standalone Segment objects can
    otherwise be parsed at its first ``{`` and silently accepted as an empty,
    action-free turn because ``SemanticInterpretation.from_payload`` supplies
    defaults for missing collections. Such output must enter schema repair (or
    fail closed), never count as a valid negative result.
    """

    keys = frozenset(str(key) for key in payload)
    missing = sorted(_MODEL_SEMANTIC_TOP_LEVEL_FIELDS - keys)
    unexpected = sorted(keys - _MODEL_SEMANTIC_TOP_LEVEL_FIELDS)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise ValueError(
            "semantic model output violates top-level contract: "
            + "; ".join(details)
        )


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
            _single_bound_pending_cancellation_payload(turn=turn, state=state)
            or _single_bound_pending_confirmation_payload(turn=turn, state=state)
            or _missing_short_confirmation_payload(
                text=turn.text,
                state=state,
                occurred_at=turn.occurred_at,
            )
            or _report_meta_opening_semantic_payload(turn.text)
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
                _validate_model_semantic_payload_shape(candidate)
                candidate = _normalize_informational_question_mutation_to_read_only(
                    candidate,
                    text=turn.text,
                )
                candidate = _normalize_informational_question_unbound_authority_refs(
                    candidate,
                    text=turn.text,
                )
                candidate = _preserve_goal_for_action_free_informational_question(
                    candidate,
                    text=turn.text,
                )
                if self._legacy_semantic_enforcers_enabled:
                    candidate = _apply_legacy_semantic_enforcers(
                        candidate,
                        turn=turn,
                        state=state,
                    )
                elif _has_asserted_daily_travel_projection(
                    candidate,
                    text=turn.text,
                ):
                    # An exact, affirmative self-trip is independently
                    # meaningful in Travel even when the model sees the active
                    # Daily dialogue and proposes only its report projection.
                    # The closed extractor cannot match questions,
                    # hypotheticals, reported speech, or cancellations, and
                    # Domain Admission still owns write authority.
                    candidate = _enforce_explicit_travel_event(
                        candidate,
                        text=turn.text,
                    )
                candidate = (
                    structured_daily_semantic_payload(turn.text)
                    or candidate
                )
                candidate = _normalize_action_free_daily_report_context(candidate)
                candidate = _normalize_daily_event_attributes(candidate)
                candidate = _normalize_travel_intent_reference_attributes(candidate)
                candidate = _normalize_daily_section_clear_binding(candidate)
                candidate = _recover_unique_bound_travel_date_correction(
                    candidate,
                    turn=turn,
                    state=state,
                )
                candidate = _normalize_unbound_travel_reference_to_clarification(
                    candidate,
                )
                candidate = _normalize_travel_update_pending_binding(candidate)
                candidate = _suppress_coupled_daily_mutation_before_travel_confirmation(
                    candidate
                )
                candidate = _normalize_multi_entity_daily_capture_actions(candidate)
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
                interpretation = _normalize_asserted_travel_source_evidence(
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
                interpretation = _project_explicit_standalone_daily_fact(
                    interpretation,
                    turn=turn,
                )
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
            _validate_model_semantic_payload_shape(candidate)
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


def _project_explicit_standalone_daily_fact(
    interpretation: SemanticInterpretation,
    *,
    turn: CognitiveTurn,
) -> SemanticInterpretation:
    """Recover independently asserted current-day facts from action-free text.

    Model segmentation remains the primary semantic boundary.  When a provider
    merges multiple strongly-delimited clauses into one action-free segment,
    each clause is evaluated independently by the existing explicit-fact
    contract.  This preserves a first-person fact beside a quotation, question,
    or hypothetical without granting the neighbouring non-fact any mutation.
    """

    result = interpretation
    projection_index = 0
    seen_projection_hashes: set[str] = set()
    for source_segment in interpretation.segments:
        if source_segment.action_ids:
            continue
        projection_candidates = _explicit_daily_projection_candidates(source_segment)
        if (
            projection_candidates
            and projection_candidates[0][0].text != source_segment.text
        ):
            result = _split_action_free_semantic_segment(
                result,
                source_segment=source_segment,
                projected_texts={
                    candidate.text for candidate, _payload in projection_candidates
                },
            )
        for candidate_segment, payload in projection_candidates:
            projection_hash = _sha256_text(candidate_segment.text)
            if projection_hash in seen_projection_hashes:
                continue
            seen_projection_hashes.add(projection_hash)
            projection_index += 1
            supplemental = _namespace_explicit_daily_fact_projection(
                SemanticInterpretation.from_payload(payload),
                source_segment=candidate_segment,
                index=projection_index,
            )
            result = _merge_independent_interpretations(result, supplemental)
    return result


_STRONG_SEMANTIC_CLAUSE_PATTERN = re.compile(
    r"[^；;\n。！？!?]+(?:[；;\n。！？!?]+|$)"
)


def _explicit_daily_projection_candidates(
    source_segment: SemanticSegment,
) -> tuple[tuple[SemanticSegment, dict[str, Any]], ...]:
    """Return only clauses that independently satisfy the Daily fact contract."""

    text = str(source_segment.text or "")
    whole_payload = explicit_standalone_daily_fact_semantic_payload(text)
    if whole_payload is not None:
        return ((source_segment, whole_payload),)

    candidates: list[tuple[SemanticSegment, dict[str, Any]]] = []
    for match in _STRONG_SEMANTIC_CLAUSE_PATTERN.finditer(text):
        raw_fragment = match.group(0)
        leading_space_count = len(raw_fragment) - len(raw_fragment.lstrip())
        trailing_space_count = len(raw_fragment) - len(raw_fragment.rstrip())
        fragment = raw_fragment.strip()
        if not fragment or fragment == text:
            continue
        payload = explicit_standalone_daily_fact_semantic_payload(fragment)
        if payload is None:
            continue

        if source_segment.start_offset is None:
            start_offset = None
            end_offset = None
        else:
            start_offset = (
                source_segment.start_offset + match.start() + leading_space_count
            )
            end_offset = (
                source_segment.start_offset + match.end() - trailing_space_count
            )
        candidates.append(
            (
                replace(
                    source_segment,
                    text=fragment,
                    text_hash=_sha256_text(fragment),
                    start_offset=start_offset,
                    end_offset=end_offset,
                ),
                payload,
            )
        )
    return tuple(candidates)


def _split_action_free_semantic_segment(
    interpretation: SemanticInterpretation,
    *,
    source_segment: SemanticSegment,
    projected_texts: set[str],
) -> SemanticInterpretation:
    """Split one provider-merged, action-free segment at strong boundaries."""

    matching_index = next(
        (
            index
            for index, segment in enumerate(interpretation.segments)
            if segment.segment_id == source_segment.segment_id
            and segment.text == source_segment.text
            and not segment.action_ids
        ),
        None,
    )


    if matching_index is None:
        return interpretation

    fragments: list[SemanticSegment] = []
    for position, match in enumerate(
        _STRONG_SEMANTIC_CLAUSE_PATTERN.finditer(source_segment.text),
        start=1,
    ):
        raw_fragment = match.group(0)
        leading_space_count = len(raw_fragment) - len(raw_fragment.lstrip())
        trailing_space_count = len(raw_fragment) - len(raw_fragment.rstrip())
        fragment = raw_fragment.strip()
        if not fragment:
            continue
        if source_segment.start_offset is None:
            start_offset = None
            end_offset = None
        else:
            start_offset = (
                source_segment.start_offset + match.start() + leading_space_count
            )
            end_offset = (
                source_segment.start_offset + match.end() - trailing_space_count
            )
        is_projected_fact = fragment in projected_texts
        fragments.append(
            replace(
                source_segment,
                segment_id=f"{source_segment.segment_id}-contract-clause-{position}",
                text=fragment,
                text_hash=_sha256_text(fragment),
                entity_ids=() if is_projected_fact else source_segment.entity_ids,
                action_ids=(),
                start_offset=start_offset,
                end_offset=end_offset,
            )
        )
    if len(fragments) < 2:
        return interpretation

    segments = list(interpretation.segments)
    segments[matching_index : matching_index + 1] = fragments
    return replace(interpretation, segments=tuple(segments))


def _namespace_explicit_daily_fact_projection(
    interpretation: SemanticInterpretation,
    *,
    source_segment: SemanticSegment,
    index: int,
) -> SemanticInterpretation:
    prefix = f"contract-explicit-daily-fact-{index}"
    entity_id_map = {
        entity.entity_id: f"{prefix}-entity-{position}"
        for position, entity in enumerate(interpretation.entities, start=1)
    }
    action_id_map = {
        action.action_id: f"{prefix}-action-{position}"
        for position, action in enumerate(
            interpretation.required_actions,
            start=1,
        )
    }
    update = interpretation.context_update
    return replace(
        interpretation,
        segments=tuple(
            replace(
                segment,
                segment_id=f"{prefix}-segment-{position}",
                text=source_segment.text,
                text_hash=_sha256_text(source_segment.text),
                entity_ids=tuple(entity_id_map[value] for value in segment.entity_ids),
                action_ids=tuple(action_id_map[value] for value in segment.action_ids),
                start_offset=source_segment.start_offset,
                end_offset=source_segment.end_offset,
            )
            for position, segment in enumerate(interpretation.segments, start=1)
        ),
        entities=tuple(
            replace(entity, entity_id=entity_id_map[entity.entity_id])
            for entity in interpretation.entities
        ),
        required_actions=tuple(
            replace(
                action,
                action_id=action_id_map[action.action_id],
                entity_ids=tuple(entity_id_map[value] for value in action.entity_ids),
            )
            for action in interpretation.required_actions
        ),
        context_update=replace(
            update,
            remember_entity_ids=tuple(
                entity_id_map[value] for value in update.remember_entity_ids
            ),
        ),
    )


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
    travel_assertion = evaluate_assertion_polarity_contract(
        domain="travel",
        segment_text=source_segment.text,
        statement_mode=(
            str(travel_entity.attributes.get("statement_mode") or "")
            or ("asserted" if extracted_travel is not None else "")
        ),
        evidence_fragments=(source_segment.text,),
        claim_anchors=(
            str(travel_entity.attributes.get("destination") or "")
            or str(travel_entity.value or ""),
        ),
    )
    if not travel_assertion.authorizes_mutation:
        return interpretation
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
    """Ground a validated explicit self-trip in the closed Runtime contract.

    Schema-invalid model output must first take the existing repair path.  Only
    after semantic validation succeeds may an exact source-text trip replace
    provider metadata with values proven by the deterministic source matcher.
    The assertion and actor-scope fields are authorization inputs, so they must
    never be discarded before Domain Admission.
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
    source_text = str(turn.text or "").strip()
    grounded_attributes = {
        "destination": extracted["destination"],
        "date_hint": extracted["date_hint"],
        "purpose": extracted["purpose"],
        "statement_mode": "asserted",
        "traveler_scope": "self",
        "evidence_spans": [[0, len(source_text)]],
    }
    normalized_entities = tuple(
        replace(
            entity,
            value=source_text,
            attributes=grounded_attributes,
        )
        if entity.entity_id == entity_id
        else entity
        for entity in interpretation.entities
    )
    return replace(interpretation, entities=normalized_entities)


def _normalize_asserted_travel_source_evidence(
    interpretation: SemanticInterpretation,
    *,
    turn: CognitiveTurn,
) -> SemanticInterpretation:
    """Anchor an already-proposed self-trip to its exact source segment.

    This does not infer a Travel action or any business value.  It only repairs
    provider evidence offsets when the proposed destination and purpose are
    both present in the one bound source segment.  Domain Admission remains
    responsible for statement polarity, date grounding and ticket issuance.
    """

    entities_by_id = {
        entity.entity_id: entity for entity in interpretation.entities
    }
    replacements: dict[str, ConversationEntity] = {}
    for action in interpretation.required_actions:
        if action.action_type != "record_travel_event" or len(action.entity_ids) != 1:
            continue
        entity = entities_by_id.get(action.entity_ids[0])
        if entity is None or entity.entity_type != "travel_event":
            continue
        if (
            str(entity.attributes.get("statement_mode") or "") != "asserted"
            or str(entity.attributes.get("traveler_scope") or "") != "self"
        ):
            continue
        matching_segments = [
            segment
            for segment in interpretation.segments
            if action.action_id in segment.action_ids
            and entity.entity_id in segment.entity_ids
            and segment.text in turn.text
        ]
        if len(matching_segments) != 1:
            continue
        source_text = matching_segments[0].text
        normalized_source = "".join(source_text.split()).casefold()
        destination = "".join(
            str(entity.attributes.get("destination") or "").split()
        ).casefold()
        purpose = "".join(
            str(entity.attributes.get("purpose") or "").split()
        ).casefold()
        if (
            not source_text
            or not destination
            or destination not in normalized_source
            or not purpose
            or purpose not in normalized_source
        ):
            continue
        replacements[entity.entity_id] = replace(
            entity,
            value=source_text,
            attributes={
                **entity.attributes,
                "evidence_spans": [[0, len(source_text)]],
            },
        )
    if not replacements:
        return interpretation
    return replace(
        interpretation,
        entities=tuple(
            replacements.get(entity.entity_id, entity)
            for entity in interpretation.entities
        ),
    )


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
                {
                    "record_travel_event",
                    "update_travel_event",
                    "respond_travel_collaboration",
                }
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


_READ_ONLY_SEMANTIC_ACTION_TYPES = frozenset(
    {
        "answer_case_query",
        "query_case_progress",
        "query_daily_report",
        "query_operation_status",
        "query_periodic_report",
        "search_enterprise_knowledge",
    }
)
_MUTATING_SEMANTIC_ACTION_TYPES = (
    frozenset(ACTION_PARAMETER_KEYS) - _READ_ONLY_SEMANTIC_ACTION_TYPES
)


def _is_informational_question(text: str) -> bool:
    source = str(text or "").strip()
    if not source:
        return False
    compact = "".join(source.split())
    if re.match(r"^(?:为什么|为何|怎么会|怎么还|怎么没|是否|有没有|有无)", compact):
        return True
    has_question_cue = bool(
        re.search(r"[?？]", source)
        or re.search(
            r"(?:为什么|为何|怎么|如何|是否|有没有|有无|什么|哪个|哪项|多少|几时|何时|吗|呢)$",
            compact,
        )
    )
    if not has_question_cue:
        return False
    explicit_action_request = bool(
        re.search(
            r"^(?:请|帮我|麻烦|劳驾|可以|能否|能不能)?(?:把|将).{0,120}"
            r"(?:改|修改|调整|删除|删掉|取消|写入|记入|添加|补充|提交|关闭|更新|移动|清空)",
            compact,
        )
        or re.search(
            r"^(?:请|帮我|麻烦|劳驾)(?:直接)?"
            r"(?:改|修改|调整|删除|删掉|取消|写入|记入|添加|补充|提交|关闭|更新|移动|清空)",
            compact,
        )
    )
    return not explicit_action_request


def _normalize_informational_question_mutation_to_read_only(
    payload: dict[str, Any],
    *,
    text: str,
) -> dict[str, Any]:
    """Prevent a direct information question from becoming a mutation candidate.

    This runs before schema validation so a provider cannot turn a question into
    an unavailable reply merely by attaching invalid mutation parameters.  A
    polite but explicit action request remains model-decided and continues
    through the normal contract and Admission checks.
    """

    actions = payload.get("required_actions")
    segments = payload.get("segments")
    if not isinstance(actions, list) or not isinstance(segments, list):
        return payload
    entity_by_id = {
        str(item.get("entity_id") or ""): item
        for item in (payload.get("entities") or ())
        if isinstance(item, dict)
    }
    segment_text_by_action: dict[str, list[str]] = {}
    for raw_segment in segments:
        if not isinstance(raw_segment, dict):
            continue
        segment_text = str(raw_segment.get("text") or "")
        for raw_action_id in raw_segment.get("action_ids") or ():
            action_id = str(raw_action_id or "").strip()
            if action_id:
                segment_text_by_action.setdefault(action_id, []).append(segment_text)
    removed_action_ids = {
        str(item.get("action_id") or "").strip()
        for item in actions
        if isinstance(item, dict)
        and str(item.get("action_type") or "") in _MUTATING_SEMANTIC_ACTION_TYPES
        and len(
            bound_texts := segment_text_by_action.get(
                str(item.get("action_id") or "").strip(),
                [],
            )
        )
        == 1
        and _is_informational_question(bound_texts[0])
        and not _is_independent_asserted_daily_capture(
            item,
            entity_by_id=entity_by_id,
            segment_text=bound_texts[0],
        )
    }
    removed_action_ids.discard("")
    if not removed_action_ids:
        return payload

    retained_actions = [
        item
        for item in actions
        if not isinstance(item, dict)
        or str(item.get("action_id") or "").strip() not in removed_action_ids
    ]
    if not retained_actions:
        return {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "contract-informational-question",
                    "text": str(text or "").strip(),
                    "intents": ["chat"],
                    "entity_ids": [],
                    "action_ids": [],
                }
            ],
            "entities": [],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }

    retained_action_ids = {
        str(item.get("action_id") or "").strip()
        for item in retained_actions
        if isinstance(item, dict) and str(item.get("action_id") or "").strip()
    }
    retained_entity_ids = {
        str(entity_id or "").strip()
        for item in retained_actions
        if isinstance(item, dict)
        for entity_id in (item.get("entity_ids") or ())
        if str(entity_id or "").strip()
    }
    retained_intents = {
        str(item.get("intent") or "").strip()
        for item in retained_actions
        if isinstance(item, dict) and str(item.get("intent") or "").strip()
    }
    retained_segments: list[dict[str, Any]] = []
    for raw_segment in segments:
        if not isinstance(raw_segment, dict):
            continue
        action_ids = [
            value
            for value in (raw_segment.get("action_ids") or ())
            if str(value or "").strip() in retained_action_ids
        ]
        if not action_ids:
            continue
        segment = dict(raw_segment)
        segment["action_ids"] = action_ids
        segment["entity_ids"] = [
            value
            for value in (raw_segment.get("entity_ids") or ())
            if str(value or "").strip() in retained_entity_ids
        ]
        segment["intents"] = [
            value
            for value in (raw_segment.get("intents") or ())
            if str(value or "").strip() in retained_intents
        ]
        retained_segments.append(segment)

    context = payload.get("context_update")
    normalized_context = dict(context) if isinstance(context, dict) else {}
    if isinstance(normalized_context.get("remember_entity_ids"), list):
        normalized_context["remember_entity_ids"] = [
            value
            for value in normalized_context["remember_entity_ids"]
            if str(value or "").strip() in retained_entity_ids
        ]
    pending = normalized_context.get("bind_pending")
    if isinstance(pending, dict) and not set(
        str(value or "").strip() for value in pending.get("entity_ids") or ()
    ).issubset(retained_entity_ids):
        normalized_context.pop("bind_pending", None)

    result = dict(payload)
    result["intents"] = [
        value
        for value in payload.get("intents") or ()
        if str(value or "").strip() in retained_intents
    ]
    result["segments"] = retained_segments
    result["entities"] = [
        item
        for item in payload.get("entities") or ()
        if isinstance(item, dict)
        and str(item.get("entity_id") or "").strip() in retained_entity_ids
    ]
    result["required_actions"] = retained_actions
    result["context_update"] = normalized_context
    if normalized_context.get("bind_pending") is None:
        result["clarification_need"] = None
    return result


def _is_independent_asserted_daily_capture(
    action: dict[str, Any],
    *,
    entity_by_id: Mapping[str, dict[str, Any]],
    segment_text: str,
) -> bool:
    """Keep one model-grounded Daily fact beside a sibling question.

    The model must already provide the Daily action, one asserted Daily entity,
    and a value that independently satisfies the closed standalone-fact
    contract.  This cannot derive a write from the question or from keywords in
    the surrounding segment.
    """

    if (
        action.get("action_type") != "capture_daily_event"
        or not isinstance(action.get("entity_ids"), list)
        or len(action["entity_ids"]) != 1
    ):
        return False
    entity = entity_by_id.get(str(action["entity_ids"][0]))
    attributes = entity.get("attributes") if isinstance(entity, dict) else None
    value = str(entity.get("value") or "").strip() if isinstance(entity, dict) else ""
    if (
        not isinstance(entity, dict)
        or entity.get("entity_type") != "daily_event"
        or not isinstance(attributes, dict)
        or str(attributes.get("statement_mode") or "") != "asserted"
        or not value
        or str(segment_text or "").count(value) != 1
    ):
        return False
    projected = explicit_standalone_daily_fact_semantic_payload(value)
    if projected is None:
        return False
    projected_entities = projected.get("entities") or ()
    if len(projected_entities) != 1 or not isinstance(projected_entities[0], dict):
        return False
    projected_attributes = projected_entities[0].get("attributes")
    return bool(
        isinstance(projected_attributes, dict)
        and str(projected_attributes.get("field") or "")
        == str(attributes.get("field") or "")
    )


_AUTHORITY_BEARING_REFERENCE_TYPES = frozenset(
    {
        "daily_item_target",
        "daily_report",
        "periodic_report",
        "report_item_target",
        "case_progress_ref",
        "case_followup_policy",
        "travel_intent_ref",
        "travel_collaboration_ref",
    }
)


def _normalize_informational_question_unbound_authority_refs(
    payload: dict[str, Any],
    *,
    text: str,
) -> dict[str, Any]:
    """Drop action-free authority references attached to a read-only question."""

    if not _is_informational_question(text):
        return payload
    actions = payload.get("required_actions")
    entities = payload.get("entities")
    segments = payload.get("segments")
    if not all(isinstance(value, list) for value in (actions, entities, segments)):
        return payload
    action_entity_ids = {
        str(entity_id or "").strip()
        for action in actions
        if isinstance(action, dict)
        for entity_id in (action.get("entity_ids") or ())
        if str(entity_id or "").strip()
    }
    removed_entity_ids = {
        str(entity.get("entity_id") or "").strip()
        for entity in entities
        if isinstance(entity, dict)
        and str(entity.get("entity_type") or "") in _AUTHORITY_BEARING_REFERENCE_TYPES
        and str(entity.get("entity_id") or "").strip()
        and str(entity.get("entity_id") or "").strip() not in action_entity_ids
    }
    if not removed_entity_ids:
        return payload

    result = dict(payload)
    result["entities"] = [
        entity
        for entity in entities
        if not isinstance(entity, dict)
        or str(entity.get("entity_id") or "").strip() not in removed_entity_ids
    ]
    normalized_segments: list[Any] = []
    for raw_segment in segments:
        if not isinstance(raw_segment, dict):
            normalized_segments.append(raw_segment)
            continue
        segment = dict(raw_segment)
        if isinstance(segment.get("entity_ids"), list):
            segment["entity_ids"] = [
                value
                for value in segment["entity_ids"]
                if str(value or "").strip() not in removed_entity_ids
            ]
        normalized_segments.append(segment)
    result["segments"] = normalized_segments

    context = payload.get("context_update")
    normalized_context = dict(context) if isinstance(context, dict) else {}
    if isinstance(normalized_context.get("remember_entity_ids"), list):
        normalized_context["remember_entity_ids"] = [
            value
            for value in normalized_context["remember_entity_ids"]
            if str(value or "").strip() not in removed_entity_ids
        ]
    pending = normalized_context.get("bind_pending")
    if isinstance(pending, dict) and removed_entity_ids.intersection(
        str(value or "").strip() for value in pending.get("entity_ids") or ()
    ):
        normalized_context.pop("bind_pending", None)
    result["context_update"] = normalized_context
    return result


def _preserve_goal_for_action_free_informational_question(
    payload: dict[str, Any],
    *,
    text: str,
) -> dict[str, Any]:
    """Keep a transient read-only question from replacing an active workflow.

    An action-free question may be answered or declined without becoming the
    user's new long-running goal.  This does not grant the previous goal any new
    authority: it emits no entity, action, Pending or Ticket and only prevents a
    provider-authored ``chat`` goal from discarding trusted dialogue continuity.
    Explicit query actions and action requests remain untouched.
    """

    if not _is_informational_question(text):
        return payload
    actions = payload.get("required_actions")
    if not isinstance(actions, list) or actions:
        return payload
    context = payload.get("context_update")
    normalized_context = dict(context) if isinstance(context, dict) else {}
    normalized_context["preserve_current_goal"] = True
    normalized_context["clear_current_goal"] = False
    normalized_context["resume_previous_goal"] = False
    result = dict(payload)
    result["context_update"] = normalized_context
    return result


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


def _normalize_unbound_travel_reference_to_clarification(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Keep an unbound Travel correction as clarification instead of an error.

    A model may correctly understand ``not tomorrow, the day after`` while no
    trusted Travel object exists.  Such a reference cannot pass the executable
    ``travel_intent_ref`` contract, but it also must not trigger schema retries
    or a generic runtime failure.  This normalization only drops unbound,
    action-free references when the model already requested clarification.
    """

    entities = payload.get("entities")
    actions = payload.get("required_actions")
    clarification = payload.get("clarification_need")
    if (
        not isinstance(entities, list)
        or not isinstance(actions, list)
        or not isinstance(clarification, dict)
    ):
        return payload
    invalid_ids: set[str] = set()
    for raw_entity in entities:
        if not isinstance(raw_entity, dict) or raw_entity.get("entity_type") != "travel_intent_ref":
            continue
        entity_id = str(raw_entity.get("entity_id") or "").strip()
        attributes = raw_entity.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        version = attributes.get("expected_version")
        if (
            entity_id
            and (
                not str(attributes.get("travel_intent_id") or "").strip()
                or not isinstance(version, int)
                or isinstance(version, bool)
                or version < 1
            )
        ):
            invalid_ids.add(entity_id)
    if not invalid_ids:
        return payload
    if any(
        invalid_ids.intersection(
            str(value or "").strip()
            for value in raw_action.get("entity_ids", [])
        )
        for raw_action in actions
        if isinstance(raw_action, dict)
        and isinstance(raw_action.get("entity_ids"), list)
    ):
        return payload

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
                if str(value or "").strip() not in invalid_ids
            ]
        normalized_segments.append(segment)

    context = payload.get("context_update")
    normalized_context = dict(context) if isinstance(context, dict) else {}
    if isinstance(normalized_context.get("remember_entity_ids"), list):
        normalized_context["remember_entity_ids"] = [
            value
            for value in normalized_context["remember_entity_ids"]
            if str(value or "").strip() not in invalid_ids
        ]
    pending = normalized_context.get("bind_pending")
    if isinstance(pending, dict) and invalid_ids.intersection(
        str(value or "").strip()
        for value in pending.get("entity_ids", [])
    ):
        normalized_context.pop("bind_pending", None)

    result = dict(payload)
    result["entities"] = [
        item
        for item in entities
        if not isinstance(item, dict)
        or str(item.get("entity_id") or "").strip() not in invalid_ids
    ]
    result["segments"] = normalized_segments
    result["context_update"] = normalized_context
    return result


_TRAVEL_DATE_CORRECTION_TOKEN = (
    r"(?:今天|今日|明天|明日|后天|"
    r"下周(?:一|二|三|四|五|六|日|天)?|"
    r"\d{4}[-年]\d{1,2}[-月]\d{1,2}日?|\d{1,2}月\d{1,2}日)"
)


def _explicit_travel_date_correction_hint(text: str) -> str:
    """Return an exact replacement date only for a closed correction grammar."""

    source = str(text or "").strip()
    if not source or re.search(r"[?？]", source):
        return ""
    if re.match(
        r"^\s*(?:如果|假如|假设|要是|听说|据说|有人说|\S{1,8}(?:说|称|表示|反馈|提到))",
        source,
    ):
        return ""
    compact = re.sub(r"[\s，,。.!！]", "", source)
    patterns = (
        rf"^(?:不对)?不是{_TRAVEL_DATE_CORRECTION_TOKEN}(?:而)?(?:是|改成|改为|调整到|挪到)(?P<new>{_TRAVEL_DATE_CORRECTION_TOKEN})$",
        rf"^(?:出差)?(?:日期|时间)?(?:改成|改为|调整到|挪到)(?P<new>{_TRAVEL_DATE_CORRECTION_TOKEN})$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, compact)
        if match is not None:
            return str(match.group("new") or "").strip()
    return ""


def _recover_unique_bound_travel_date_correction(
    payload: dict[str, Any],
    *,
    turn: CognitiveTurn,
    state: ConversationState,
) -> dict[str, Any]:
    """Canonicalize one explicit correction when trusted context proves its target.

    The model remains responsible for ordinary semantic interpretation.  This
    fail-closed recovery applies to either a model no-op or a model-proposed
    Travel confirmation, and only for an explicit date-correction grammar, an
    active Travel dialogue, and exactly one permission-filtered active item. It
    re-anchors provider metadata to the exact source and trusted resource.  It
    grants no write: it can only request the existing medium-risk confirmation
    protocol, whose object/version/date binding is revalidated by Admission.
    """

    actions = payload.get("required_actions")
    context = payload.get("context_update")
    clarification = payload.get("clarification_need")
    if not isinstance(actions, list):
        return payload
    if str(getattr(state.current_goal, "intent", "") or "") != "travel_event":
        return payload
    new_date_hint = _explicit_travel_date_correction_hint(turn.text)
    if not new_date_hint:
        return payload
    active_rows = [
        dict(item)
        for item in turn.resources.get("active_travel_intents") or ()
        if isinstance(item, Mapping)
        and str(item.get("status") or "") not in {"cancelled", "completed"}
    ]
    if len(active_rows) != 1:
        return payload
    active = active_rows[0]
    travel_intent_id = str(active.get("travel_intent_id") or "").strip()
    destination = str(active.get("destination") or "").strip()
    expected_version = active.get("version")
    if (
        not travel_intent_id
        or not destination
        or not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 1
    ):
        return payload

    existing_pending = context.get("bind_pending") if isinstance(context, dict) else None
    if existing_pending is not None:
        if (
            not isinstance(existing_pending, dict)
            or existing_pending.get("action") != "update_travel_event"
        ):
            return payload
    elif clarification is not None:
        return payload

    entity_id = "contract-bound-travel-date-correction"
    segment_id = "contract-bound-travel-date-correction-segment"
    return {
        "intents": ["travel_event"],
        "segments": [
            {
                "segment_id": segment_id,
                "text": str(turn.text or "").strip(),
                "intents": ["travel_event"],
                "entity_ids": [entity_id],
                "action_ids": [],
            }
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "entity_type": "travel_intent_ref",
                "value": destination,
                "confidence": 1.0,
                "attributes": {
                    "travel_intent_id": travel_intent_id,
                    "expected_version": expected_version,
                    "destination": destination,
                    "new_date_hint": new_date_hint,
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": {
            "reason": "medium_risk_confirmation_required",
            "missing_fields": ["confirmation"],
            "question": "确认修改这项出差的日期吗？",
        },
        "context_update": {
            "preserve_current_goal": True,
            "remember_turn": True,
            "bind_pending": {
                "pending_id": "contract-bound-travel-date-correction",
                "intent": "travel_event",
                "action": "update_travel_event",
                "entity_ids": [entity_id],
                "expires_in_seconds": 600,
            },
        },
    }


def _normalize_travel_update_pending_binding(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Narrow one Travel confirmation pending to its sole Travel reference.

    Models sometimes include an independently described Daily projection in
    the pending's entity list.  Removing that sibling cannot grant authority;
    it restores the reviewed one-object confirmation contract while all object
    ID/version checks remain in trusted Admission.
    """

    context = payload.get("context_update")
    if not isinstance(context, dict):
        return payload
    pending = context.get("bind_pending")
    if not isinstance(pending, dict) or pending.get("action") != "update_travel_event":
        return payload
    pending_ids = [
        str(value or "").strip()
        for value in pending.get("entity_ids", [])
        if str(value or "").strip()
    ] if isinstance(pending.get("entity_ids"), list) else []
    travel_ids = [
        str(item.get("entity_id") or "").strip()
        for item in payload.get("entities", [])
        if isinstance(item, dict)
        and item.get("entity_type") == "travel_intent_ref"
        and str(item.get("entity_id") or "").strip() in pending_ids
    ]
    if len(travel_ids) != 1:
        return payload
    travel_id = travel_ids[0]
    normalized_pending = dict(pending)
    normalized_pending["entity_ids"] = travel_ids
    normalized_context = dict(context)
    normalized_context["bind_pending"] = normalized_pending

    # A provider can propose the right one-object confirmation while omitting
    # one side of the segment reference (intent or entity).  Repair only a
    # unique structural target; Domain Admission still re-binds the object,
    # version, source text, requested change and risk class from trusted
    # resources before persisting any Pending.
    segments = payload.get("segments")
    normalized_segments = list(segments) if isinstance(segments, list) else []

    def _segment_values(item: dict[str, Any], key: str) -> set[str]:
        values = item.get(key)
        if not isinstance(values, list):
            return set()
        return {
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        }

    exact_matches = [
        index
        for index, item in enumerate(normalized_segments)
        if isinstance(item, dict)
        and travel_id in _segment_values(item, "entity_ids")
        and str(pending.get("intent") or "")
        in _segment_values(item, "intents")
    ]
    if not exact_matches:
        entity_matches = [
            index
            for index, item in enumerate(normalized_segments)
            if isinstance(item, dict)
            and travel_id in _segment_values(item, "entity_ids")
        ]
        intent_matches = [
            index
            for index, item in enumerate(normalized_segments)
            if isinstance(item, dict)
            and str(pending.get("intent") or "")
            in _segment_values(item, "intents")
        ]
        repair_index: int | None = None
        if len(entity_matches) == 1:
            repair_index = entity_matches[0]
        elif len(intent_matches) == 1:
            repair_index = intent_matches[0]
        elif len(normalized_segments) == 1:
            repair_index = 0
        if repair_index is not None and isinstance(
            normalized_segments[repair_index], dict
        ):
            segment = dict(normalized_segments[repair_index])
            existing_entity_ids = segment.get("entity_ids")
            existing_intents = segment.get("intents")
            segment["entity_ids"] = list(
                dict.fromkeys(
                    [
                        *(
                            existing_entity_ids
                            if isinstance(existing_entity_ids, list)
                            else []
                        ),
                        travel_id,
                    ]
                )
            )
            segment["intents"] = list(
                dict.fromkeys(
                    [
                        *(
                            existing_intents
                            if isinstance(existing_intents, list)
                            else []
                        ),
                        str(pending.get("intent") or ""),
                    ]
                )
            )
            normalized_segments[repair_index] = segment
    result = dict(payload)
    result["context_update"] = normalized_context
    if isinstance(segments, list):
        result["segments"] = normalized_segments
    return result


def _suppress_coupled_daily_mutation_before_travel_confirmation(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Keep a Travel confirmation turn non-mutating until it is confirmed.

    A model can describe a Daily projection edit in the same segment as a
    medium-risk Travel correction.  That projection has no independent
    authority and must not either execute early or prevent the trusted Travel
    pending from being persisted.  Independent Daily segments remain intact.
    """

    context = payload.get("context_update")
    if not isinstance(context, dict):
        return payload
    pending = context.get("bind_pending")
    if (
        not isinstance(pending, dict)
        or pending.get("action") != "update_travel_event"
    ):
        return payload
    pending_ids = {
        str(value or "").strip()
        for value in pending.get("entity_ids", [])
        if str(value or "").strip()
    } if isinstance(pending.get("entity_ids"), list) else set()
    if len(pending_ids) != 1:
        return payload

    raw_entities = payload.get("entities")
    raw_actions = payload.get("required_actions")
    raw_segments = payload.get("segments")
    if not all(isinstance(value, list) for value in (raw_entities, raw_actions, raw_segments)):
        return payload
    entity_types = {
        str(item.get("entity_id") or "").strip(): str(
            item.get("entity_type") or ""
        ).strip()
        for item in raw_entities
        if isinstance(item, dict) and str(item.get("entity_id") or "").strip()
    }
    if any(entity_types.get(entity_id) != "travel_intent_ref" for entity_id in pending_ids):
        return payload

    travel_segments: list[dict[str, Any]] = []
    coupled_segment_action_ids: set[str] = set()
    coupled_projection_segments: set[int] = set()
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            continue
        segment_entity_ids = {
            str(value or "").strip()
            for value in raw_segment.get("entity_ids", [])
            if str(value or "").strip()
        } if isinstance(raw_segment.get("entity_ids"), list) else set()
        if not pending_ids.intersection(segment_entity_ids):
            continue
        travel_segments.append(raw_segment)
        coupled_segment_action_ids.update(
            str(value or "").strip()
            for value in raw_segment.get("action_ids", [])
            if str(value or "").strip()
        )

    # Sometimes the provider duplicates one source span into a Travel segment
    # and a Daily projection segment.  Treat overlapping/equal source spans as
    # one coupled confirmation; genuinely separate Daily segments remain
    # executable independently.
    if len(travel_segments) == 1:
        travel_segment = travel_segments[0]
        travel_text = str(travel_segment.get("text") or "").strip()
        travel_start = travel_segment.get("start_offset")
        travel_end = travel_segment.get("end_offset")
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, dict) or raw_segment is travel_segment:
                continue
            segment_text = str(raw_segment.get("text") or "").strip()
            segment_start = raw_segment.get("start_offset")
            segment_end = raw_segment.get("end_offset")
            equal_source = bool(travel_text and segment_text == travel_text)
            overlapping_source = (
                isinstance(travel_start, int)
                and not isinstance(travel_start, bool)
                and isinstance(travel_end, int)
                and not isinstance(travel_end, bool)
                and isinstance(segment_start, int)
                and not isinstance(segment_start, bool)
                and isinstance(segment_end, int)
                and not isinstance(segment_end, bool)
                and max(travel_start, segment_start) < min(travel_end, segment_end)
            )
            if not (equal_source or overlapping_source):
                continue
            coupled_projection_segments.add(id(raw_segment))
            coupled_segment_action_ids.update(
                str(value or "").strip()
                for value in raw_segment.get("action_ids", [])
                if str(value or "").strip()
            )

    daily_mutations = {
        "capture_daily_event",
        "edit_daily_item",
        "delete_daily_item",
        "merge_daily_items",
        "replace_daily_section",
        "move_daily_items",
        "clear_daily_section",
        "clear_daily_report",
        "copy_previous_daily_report",
        "copy_current_work_to_tomorrow",
        "complete_previous_daily_plan",
    }
    removed_action_ids: set[str] = set()
    daily_projection_entity_types = {
        "daily_event",
        "daily_item_target",
        "daily_report",
    }
    removed_entity_ids: set[str] = {
        str(value or "").strip()
        for segment in raw_segments
        if isinstance(segment, dict)
        and (
            segment in travel_segments
            or id(segment) in coupled_projection_segments
        )
        and isinstance(segment.get("entity_ids"), list)
        for value in segment["entity_ids"]
        if entity_types.get(str(value or "").strip())
        in daily_projection_entity_types
    }
    normalized_actions: list[Any] = []
    for raw_action in raw_actions:
        if not isinstance(raw_action, dict):
            normalized_actions.append(raw_action)
            continue
        action_id = str(raw_action.get("action_id") or "").strip()
        action_type = str(raw_action.get("action_type") or "").strip()
        action_entity_ids = {
            str(value or "").strip()
            for value in raw_action.get("entity_ids", [])
            if str(value or "").strip()
        } if isinstance(raw_action.get("entity_ids"), list) else set()
        if action_type in daily_mutations and (
            action_id in coupled_segment_action_ids
            or bool(action_entity_ids.intersection(removed_entity_ids))
        ):
            removed_action_ids.add(action_id)
            removed_entity_ids.update(action_entity_ids)
            continue
        normalized_actions.append(raw_action)
    if not removed_action_ids and not removed_entity_ids:
        return payload

    retained_entity_ids = {
        str(value or "").strip()
        for action in normalized_actions
        if isinstance(action, dict) and isinstance(action.get("entity_ids"), list)
        for value in action["entity_ids"]
        if str(value or "").strip()
    } | pending_ids
    removable_entity_ids = removed_entity_ids - retained_entity_ids
    normalized_segments: list[Any] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            normalized_segments.append(raw_segment)
            continue
        segment = dict(raw_segment)
        if isinstance(segment.get("action_ids"), list):
            segment["action_ids"] = [
                value
                for value in segment["action_ids"]
                if str(value or "").strip() not in removed_action_ids
            ]
        if isinstance(segment.get("entity_ids"), list):
            segment["entity_ids"] = [
                value
                for value in segment["entity_ids"]
                if str(value or "").strip() not in removable_entity_ids
            ]
        if (
            id(raw_segment) in coupled_projection_segments
            and not segment.get("action_ids")
            and not segment.get("entity_ids")
        ):
            continue
        normalized_segments.append(segment)

    normalized_context = dict(context)
    if isinstance(normalized_context.get("remember_entity_ids"), list):
        normalized_context["remember_entity_ids"] = [
            value
            for value in normalized_context["remember_entity_ids"]
            if str(value or "").strip() not in removable_entity_ids
        ]
    result = dict(payload)
    result["entities"] = [
        item
        for item in raw_entities
        if not isinstance(item, dict)
        or str(item.get("entity_id") or "").strip() not in removable_entity_ids
    ]
    result["required_actions"] = normalized_actions
    result["segments"] = normalized_segments
    result["context_update"] = normalized_context
    represented_intents = {
        str(value or "").strip()
        for segment in normalized_segments
        if isinstance(segment, dict) and isinstance(segment.get("intents"), list)
        for value in segment["intents"]
        if str(value or "").strip()
    } | {
        str(action.get("intent") or "").strip()
        for action in normalized_actions
        if isinstance(action, dict) and str(action.get("intent") or "").strip()
    } | {str(pending.get("intent") or "").strip()}
    if isinstance(payload.get("intents"), list):
        result["intents"] = [
            value
            for value in payload["intents"]
            if str(value or "").strip() in represented_intents
        ]
    return result


def _normalize_multi_entity_daily_capture_actions(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Bind each Daily capture to one proposed ``daily_event`` entity.

    No entity, field, value, or operation is inferred here.  The normalization
    only preserves already-proposed ``daily_event`` bindings, drops unrelated
    context entities accidentally attached to the action, and makes the
    one-entity cardinality explicit for Admission and Typed Commands.  Context
    entities remain available to the segment/state; they gain no write authority.
    """

    raw_entities = payload.get("entities")
    raw_actions = payload.get("required_actions")
    raw_segments = payload.get("segments")
    if (
        not isinstance(raw_entities, list)
        or not isinstance(raw_actions, list)
        or not isinstance(raw_segments, list)
    ):
        return payload
    entity_types = {
        str(item.get("entity_id") or "").strip(): str(
            item.get("entity_type") or ""
        ).strip()
        for item in raw_entities
        if isinstance(item, dict) and str(item.get("entity_id") or "").strip()
    }
    existing_action_ids = {
        str(item.get("action_id") or "").strip()
        for item in raw_actions
        if isinstance(item, dict) and str(item.get("action_id") or "").strip()
    }
    replacements: dict[str, dict[str, str]] = {}
    normalized_actions: list[Any] = []
    changed = False
    for raw_action in raw_actions:
        if not isinstance(raw_action, dict):
            normalized_actions.append(raw_action)
            continue
        action_id = str(raw_action.get("action_id") or "").strip()
        entity_ids = [
            str(value or "").strip()
            for value in raw_action.get("entity_ids", [])
            if str(value or "").strip()
        ] if isinstance(raw_action.get("entity_ids"), list) else []
        if raw_action.get("action_type") != "capture_daily_event" or not action_id:
            normalized_actions.append(raw_action)
            continue
        daily_entity_ids = [
            entity_id
            for entity_id in entity_ids
            if entity_types.get(entity_id) == "daily_event"
        ]
        if not daily_entity_ids:
            normalized_actions.append(raw_action)
            continue
        if len(daily_entity_ids) == 1:
            if daily_entity_ids == entity_ids:
                normalized_actions.append(raw_action)
            else:
                action = dict(raw_action)
                action["entity_ids"] = daily_entity_ids
                normalized_actions.append(action)
                changed = True
            continue
        replacements[action_id] = {}
        changed = True
        for index, entity_id in enumerate(daily_entity_ids, start=1):
            candidate_id = f"{action_id}-entity-{index}"
            suffix = index
            while candidate_id in existing_action_ids:
                suffix += 1
                candidate_id = f"{action_id}-entity-{suffix}"
            existing_action_ids.add(candidate_id)
            replacements[action_id][entity_id] = candidate_id
            action = dict(raw_action)
            action["action_id"] = candidate_id
            action["entity_ids"] = [entity_id]
            normalized_actions.append(action)
    if not changed:
        return payload

    if not replacements:
        result = dict(payload)
        result["required_actions"] = normalized_actions
        return result

    normalized_segments: list[Any] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            normalized_segments.append(raw_segment)
            continue
        segment = dict(raw_segment)
        segment_entities = {
            str(value or "").strip()
            for value in segment.get("entity_ids", [])
            if str(value or "").strip()
        } if isinstance(segment.get("entity_ids"), list) else set()
        action_ids: list[str] = []
        for raw_id in segment.get("action_ids", []):
            action_id = str(raw_id or "").strip()
            replacement = replacements.get(action_id)
            if replacement is None:
                if action_id:
                    action_ids.append(action_id)
                continue
            action_ids.extend(
                new_id
                for entity_id, new_id in replacement.items()
                if entity_id in segment_entities
            )
        segment["action_ids"] = action_ids
        normalized_segments.append(segment)

    result = dict(payload)
    result["required_actions"] = normalized_actions
    result["segments"] = normalized_segments
    return result


def _normalize_daily_section_clear_binding(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Repair one closed provider-shape error without inferring user intent.

    ``clear_daily_section`` is already an explicit model decision, but providers
    sometimes bind both a ``daily_item_target`` (which carries ``target_field``)
    and the trusted ``daily_report`` snapshot.  The semantic contract requires
    exactly the report entity with ``attributes.field``.  Copying one
    non-conflicting closed field and dropping only the extraneous action binding
    preserves the provider's decision while leaving Admission in charge of
    grounding and write authority.  Missing, conflicting, or unknown fields stay
    invalid and therefore fail closed inside the normal repair loop.
    """

    raw_entities = payload.get("entities")
    raw_actions = payload.get("required_actions")
    if not isinstance(raw_entities, list) or not isinstance(raw_actions, list):
        return payload
    entity_by_id = {
        str(entity.get("entity_id") or "").strip(): entity
        for entity in raw_entities
        if isinstance(entity, dict) and str(entity.get("entity_id") or "").strip()
    }
    valid_fields = {"today_work", "problems", "tomorrow_plan"}
    normalized_entities = list(raw_entities)
    normalized_actions: list[Any] = []
    repaired_target_ids: set[str] = set()
    changed = False

    for raw_action in raw_actions:
        if (
            not isinstance(raw_action, dict)
            or raw_action.get("action_type") != "clear_daily_section"
            or not isinstance(raw_action.get("entity_ids"), list)
        ):
            normalized_actions.append(raw_action)
            continue
        bound_ids = [
            str(value or "").strip()
            for value in raw_action["entity_ids"]
            if str(value or "").strip()
        ]
        report_ids = [
            entity_id
            for entity_id in bound_ids
            if entity_by_id.get(entity_id, {}).get("entity_type") == "daily_report"
        ]
        target_ids = [
            entity_id
            for entity_id in bound_ids
            if entity_by_id.get(entity_id, {}).get("entity_type") == "daily_item_target"
        ]
        if (
            len(report_ids) != 1
            or not target_ids
            or len(report_ids) + len(target_ids) != len(bound_ids)
        ):
            normalized_actions.append(raw_action)
            continue

        report = entity_by_id[report_ids[0]]
        report_attributes = report.get("attributes")
        if not isinstance(report_attributes, dict):
            normalized_actions.append(raw_action)
            continue
        report_field = str(report_attributes.get("field") or "").strip()
        target_fields = {
            str(attributes.get("target_field") or "").strip()
            for target_id in target_ids
            if isinstance(
                attributes := entity_by_id[target_id].get("attributes"),
                dict,
            )
            and str(attributes.get("target_field") or "").strip()
        }
        if report_field:
            if report_field not in valid_fields or target_fields != {report_field}:
                normalized_actions.append(raw_action)
                continue
        elif len(target_fields) == 1 and target_fields <= valid_fields:
            report_field = next(iter(target_fields))
            normalized_report = dict(report)
            normalized_report_attributes = dict(report_attributes)
            normalized_report_attributes["field"] = report_field
            normalized_report["attributes"] = normalized_report_attributes
            report_index = normalized_entities.index(report)
            normalized_entities[report_index] = normalized_report
            entity_by_id[report_ids[0]] = normalized_report
        else:
            normalized_actions.append(raw_action)
            continue

        normalized_action = dict(raw_action)
        normalized_action["entity_ids"] = report_ids
        normalized_actions.append(normalized_action)
        repaired_target_ids.update(target_ids)
        changed = True

    if not changed:
        return payload

    authoritative_ids = {
        str(entity_id or "").strip()
        for action in normalized_actions
        if isinstance(action, dict) and isinstance(action.get("entity_ids"), list)
        for entity_id in action["entity_ids"]
        if str(entity_id or "").strip()
    }
    context = payload.get("context_update")
    normalized_context = dict(context) if isinstance(context, dict) else context
    pending = context.get("bind_pending") if isinstance(context, dict) else None
    if isinstance(pending, dict) and isinstance(pending.get("entity_ids"), list):
        authoritative_ids.update(
            str(entity_id or "").strip()
            for entity_id in pending["entity_ids"]
            if str(entity_id or "").strip()
        )
    removable_from_memory = repaired_target_ids - authoritative_ids
    if (
        isinstance(normalized_context, dict)
        and isinstance(normalized_context.get("remember_entity_ids"), list)
    ):
        normalized_context["remember_entity_ids"] = [
            entity_id
            for entity_id in normalized_context["remember_entity_ids"]
            if str(entity_id or "").strip() not in removable_from_memory
        ]

    result = dict(payload)
    result["entities"] = normalized_entities
    result["required_actions"] = normalized_actions
    if isinstance(normalized_context, dict):
        result["context_update"] = normalized_context
    return result


def _normalize_action_free_daily_report_context(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Remove malformed embedded items from a non-authoritative report ref.

    Providers sometimes attach display-shaped dictionaries to ``items`` on a
    context-only ``daily_report`` entity.  The closed contract accepts only a
    non-empty list of strings, and repeating the malformed context on repair
    would otherwise make an action-free turn unavailable.  This normalization
    is deliberately one-way and authority-reducing: it never changes an entity
    referenced by an action or Pending, never creates an item, and preserves the
    report ID/version/date used for read-only context.
    """

    entities = payload.get("entities")
    actions = payload.get("required_actions")
    if not isinstance(entities, list) or not isinstance(actions, list):
        return payload
    authoritative_entity_ids = {
        str(entity_id or "").strip()
        for action in actions
        if isinstance(action, dict)
        for entity_id in (action.get("entity_ids") or ())
        if str(entity_id or "").strip()
    }
    context = payload.get("context_update")
    pending = context.get("bind_pending") if isinstance(context, dict) else None
    if isinstance(pending, dict):
        authoritative_entity_ids.update(
            str(entity_id or "").strip()
            for entity_id in (pending.get("entity_ids") or ())
            if str(entity_id or "").strip()
        )

    changed = False
    normalized_entities: list[Any] = []
    for raw_entity in entities:
        if (
            not isinstance(raw_entity, dict)
            or raw_entity.get("entity_type") != "daily_report"
            or str(raw_entity.get("entity_id") or "").strip()
            in authoritative_entity_ids
        ):
            normalized_entities.append(raw_entity)
            continue
        attributes = raw_entity.get("attributes")
        if not isinstance(attributes, dict) or "items" not in attributes:
            normalized_entities.append(raw_entity)
            continue
        items = attributes.get("items")
        valid_items = bool(
            isinstance(items, (list, tuple))
            and items
            and all(isinstance(value, str) and value.strip() for value in items)
        )
        if valid_items:
            normalized_entities.append(raw_entity)
            continue
        normalized_attributes = dict(attributes)
        normalized_attributes.pop("items", None)
        entity = dict(raw_entity)
        entity["attributes"] = normalized_attributes
        normalized_entities.append(entity)
        changed = True
    if not changed:
        return payload
    result = dict(payload)
    result["entities"] = normalized_entities
    return result


def _normalize_daily_event_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Discard model-only provenance fields outside the Daily entity contract.

    Daily authority is grounded from the exact entity value and source segment;
    unlike Case and Travel, ``daily_event`` does not accept ``evidence_spans``.
    Removing only that known extraneous key prevents schema-repair loops without
    creating an entity, action, field, or write authority.
    """

    raw_entities = payload.get("entities")
    if not isinstance(raw_entities, list):
        return payload
    changed = False
    entities: list[Any] = []
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict) or raw_entity.get("entity_type") != "daily_event":
            entities.append(raw_entity)
            continue
        attributes = raw_entity.get("attributes")
        if not isinstance(attributes, dict) or "evidence_spans" not in attributes:
            entities.append(raw_entity)
            continue
        normalized_attributes = dict(attributes)
        normalized_attributes.pop("evidence_spans", None)
        entity = dict(raw_entity)
        entity["attributes"] = normalized_attributes
        entities.append(entity)
        changed = True
    if not changed:
        return payload
    result = dict(payload)
    result["entities"] = entities
    return result


def _normalize_travel_intent_reference_attributes(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Discard provider provenance that cannot authorize a Travel update.

    ``travel_intent_ref`` is grounded exclusively by the permission-filtered
    stable ID, optimistic version, requested change and source segment.  Some
    providers copy ``evidence_spans`` from the separate ``travel_event`` schema;
    removing only that known non-authoritative key avoids a repair loop while
    retaining the closed contract for every other unexpected attribute.
    """

    raw_entities = payload.get("entities")
    if not isinstance(raw_entities, list):
        return payload
    changed = False
    entities: list[Any] = []
    for raw_entity in raw_entities:
        if (
            not isinstance(raw_entity, dict)
            or raw_entity.get("entity_type") != "travel_intent_ref"
        ):
            entities.append(raw_entity)
            continue
        attributes = raw_entity.get("attributes")
        if not isinstance(attributes, dict) or "evidence_spans" not in attributes:
            entities.append(raw_entity)
            continue
        normalized_attributes = dict(attributes)
        normalized_attributes.pop("evidence_spans", None)
        entity = dict(raw_entity)
        entity["attributes"] = normalized_attributes
        entities.append(entity)
        changed = True
    if not changed:
        return payload
    result = dict(payload)
    result["entities"] = entities
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


def _has_asserted_daily_travel_projection(
    payload: dict[str, Any],
    *,
    text: str,
) -> bool:
    """Require an existing model-proposed Daily fact before adding Travel.

    This keeps Admission mode from inventing a mutation out of keyword-only
    chat.  It only restores the parallel Travel facet when the model already
    asserted the complete turn as a ``tomorrow_plan`` Daily projection.
    """

    if payload.get("clarification_need") is not None:
        return False
    source = str(text or "").strip()
    if not source or _extract_explicit_travel_event(source) is None:
        return False
    raw_entities = payload.get("entities")
    raw_actions = payload.get("required_actions")
    raw_segments = payload.get("segments")
    if not all(
        isinstance(value, list)
        for value in (raw_entities, raw_actions, raw_segments)
    ):
        return False
    entity_by_id = {
        str(item.get("entity_id") or ""): item
        for item in raw_entities
        if isinstance(item, dict)
    }
    for action in raw_actions:
        if (
            not isinstance(action, dict)
            or action.get("action_type") != "capture_daily_event"
            or not isinstance(action.get("entity_ids"), list)
            or len(action["entity_ids"]) != 1
        ):
            continue
        entity_id = str(action["entity_ids"][0])
        entity = entity_by_id.get(entity_id)
        attributes = entity.get("attributes") if isinstance(entity, dict) else None
        if (
            not isinstance(entity, dict)
            or entity.get("entity_type") != "daily_event"
            or str(entity.get("value") or "").strip() != source
            or not isinstance(attributes, dict)
            or str(attributes.get("field") or "") != "tomorrow_plan"
            or str(attributes.get("statement_mode") or "") != "asserted"
        ):
            continue
        action_id = str(action.get("action_id") or "")
        bound_segments = [
            segment
            for segment in raw_segments
            if isinstance(segment, dict)
            and action_id in (segment.get("action_ids") or [])
            and entity_id in (segment.get("entity_ids") or [])
            and str(segment.get("text") or "").strip() == source
        ]
        if len(bound_segments) == 1:
            return True
    return False


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
        and str(draft.get("status") or "") in {"collecting", "pending_confirmation"}
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
            rf")?{label}(?:了|啦|咯|啊|呀|吧|呢|哈|嘛|呗)?",
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
            and status
            in {
                "active",
                "collecting",
                "in_progress",
                "pending",
                "pending_confirmation",
            }
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
    if re.search(r"[“‘\"].+?[”’\"]", source):
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
    if re.search(
        r"(?:没|没有|未|尚未|还没|并未).{0,6}"
        r"(?:完成|做完|处理|审核|推进|跟进|整理|制作|参加|提交)",
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
    return _short_confirmation_mismatch_payload(text)


def _missing_short_confirmation_payload(
    *,
    text: str,
    state: ConversationState,
    occurred_at: datetime,
) -> dict[str, Any] | None:
    """Resolve a bare acknowledgement with no active authority locally."""

    if (
        not _is_short_confirmation(text)
        or state.active_pending(occurred_at)
    ):
        return None
    return _short_confirmation_mismatch_payload(text)


def _short_confirmation_mismatch_payload(text: str) -> dict[str, Any]:
    """Return the closed zero-write response for an unbound acknowledgement."""

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
    return is_explicit_pending_confirmation(text)


def _single_bound_pending_cancellation_payload(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
) -> dict[str, Any] | None:
    """Recognize only the closed cancellation protocol for one active pending."""

    if not is_explicit_pending_cancellation(turn.text):
        return None
    if len(state.active_pending(turn.occurred_at)) != 1:
        return None
    return {
        "intents": ["pending_cancel"],
        "segments": [
            {
                "segment_id": "bound-pending-cancellation",
                "text": turn.text,
                "intents": ["pending_cancel"],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"preserve_current_goal": True},
    }


def _single_bound_pending_confirmation_payload(
    *,
    turn: CognitiveTurn,
    state: ConversationState,
) -> dict[str, Any] | None:
    """Resolve an exact confirmation from one trusted pending without model recopy.

    The model is not asked to reproduce stable IDs, optimistic versions, or the
    bound action.  Cognitive Core still revalidates this continuation against
    the active state before trusted Admission can authorize anything.
    """

    if not is_explicit_pending_confirmation(turn.text):
        return None
    active = state.active_pending(turn.occurred_at)
    if len(active) != 1:
        return None
    pending = active[0]
    by_id = {entity.entity_id: entity for entity in state.current_entities}
    entities = [by_id.get(entity_id) for entity_id in pending.entity_ids]
    if any(entity is None for entity in entities):
        return None
    action_id = "continue-bound-pending"
    return {
        "intents": [pending.intent],
        "segments": [
            {
                "segment_id": "bound-pending-confirmation",
                "text": turn.text,
                "intents": [pending.intent],
                "entity_ids": list(pending.entity_ids),
                "action_ids": [action_id],
            }
        ],
        "entities": [
            {
                "entity_id": entity.entity_id,
                "entity_type": entity.entity_type,
                "value": entity.value,
                "confidence": entity.confidence,
                "attributes": dict(entity.attributes),
            }
            for entity in entities
            if entity is not None
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": action_id,
                "action_type": "continue_pending",
                "intent": pending.intent,
                "entity_ids": list(pending.entity_ids),
                "parameters": {
                    "pending_id": pending.pending_id,
                    "bound_action": pending.action,
                },
            }
        ],
        "clarification_need": None,
        "context_update": {},
    }


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
        if item.get("entity_type") in {"case_progress_ref", "travel_intent_ref"}:
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
        and str(draft.get("status") or "") in {"collecting", "pending_confirmation"}
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
