from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping
from uuid import UUID

from app.agent2.command_planner_v3 import TypedBusinessCommand
from app.agent2.selection_pending import SelectionCandidate, SelectionPending


@dataclass(frozen=True)
class SelectedCommandSnapshot:
    """Protected original command plus the one server-selected binding.

    This snapshot is deliberately non-executable.  It gives fresh Admission a
    canonical view of the facts that came from the original turn without
    treating the current ordinal answer as new case-progress content.
    """

    raw_command: dict[str, Any]
    payload: dict[str, Any]
    entity: dict[str, Any]
    source_segment: dict[str, Any]
    original_continuation_sha256: str
    original_source_digest: str
    bound_payload_sha256: str


def selected_business_command_snapshot(
    pending: SelectionPending,
    candidate: SelectionCandidate,
) -> SelectedCommandSnapshot:
    raw = deepcopy(dict((pending.continuation_payload or {}).get("typed_business_command") or {}))
    if not raw:
        raise ValueError("selection pending has no typed business continuation")
    payload = deepcopy(dict(raw.get("payload") or {}))
    entities = payload.get("entities")
    if not isinstance(entities, list) or len(entities) != 1 or not isinstance(entities[0], dict):
        raise ValueError("selection continuation requires one structured entity")
    bind = dict((pending.continuation_payload or {}).get("bind") or {})
    expected_type = str(bind.get("entity_type") or "")
    entity = deepcopy(dict(entities[0]))
    if expected_type and str(entity.get("entity_type") or "") != expected_type:
        raise ValueError("selection continuation entity type changed")
    entity["value"] = candidate.stable_id
    attributes = deepcopy(dict(entity.get("attributes") or {}))
    attribute = str(bind.get("attribute") or "")
    if attribute:
        attributes[attribute] = candidate.stable_id
    entity["attributes"] = attributes
    entities[0] = entity
    payload["entities"] = entities

    segments = payload.get("source_segments")
    if (
        not isinstance(segments, list)
        or len(segments) != 1
        or not isinstance(segments[0], dict)
    ):
        raise ValueError("selection continuation requires one protected source segment")
    source_segment = deepcopy(dict(segments[0]))
    source_text = str(source_segment.get("text") or "")
    source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if not source_text.strip() or str(source_segment.get("text_hash") or "") != source_digest:
        raise ValueError("selection continuation source digest changed")
    try:
        start_offset = int(source_segment.get("start_offset"))
        end_offset = int(source_segment.get("end_offset"))
    except (TypeError, ValueError) as exc:
        raise ValueError("selection continuation source offsets missing") from exc
    if start_offset < 0 or end_offset - start_offset != len(source_text):
        raise ValueError("selection continuation source offsets changed")

    protected = (pending.continuation_payload or {}).get("protected_snapshot")
    original_payload = raw.get("payload")
    if (
        not isinstance(protected, dict)
        or str(protected.get("original_continuation_sha256") or "")
        != _sha256_json(raw)
        or str(protected.get("original_payload_sha256") or "")
        != _sha256_json(original_payload)
        or str(protected.get("source_segment_id") or "")
        != str(source_segment.get("segment_id") or "")
        or str(protected.get("original_source_digest") or "") != source_digest
        or int(protected.get("source_start_offset", -1)) != start_offset
        or int(protected.get("source_end_offset", -1)) != end_offset
    ):
        raise ValueError("selection protected snapshot changed")

    return SelectedCommandSnapshot(
        raw_command=raw,
        payload=payload,
        entity=entity,
        source_segment=source_segment,
        original_continuation_sha256=_sha256_json(raw),
        original_source_digest=source_digest,
        bound_payload_sha256=_sha256_json(payload),
    )


def bind_selected_business_command(
    pending: SelectionPending,
    candidate: SelectionCandidate,
    *,
    admission_ticket: Mapping[str, Any] | None = None,
    selection_evidence: Mapping[str, Any] | None = None,
) -> TypedBusinessCommand:
    """Bind a selected stable id; without fresh Admission it is non-executable.

    The legacy two-argument call remains loadable for old serialized Pending
    objects, but it now sets ``admission_required`` with an empty ticket.  All
    production compilers/executors therefore fail closed instead of executing
    a saved command merely because a candidate was selected.
    """

    snapshot = selected_business_command_snapshot(pending, candidate)
    raw = snapshot.raw_command
    payload = deepcopy(snapshot.payload)
    ticket = dict(admission_ticket or {})
    if ticket:
        authority = ticket.get("authority_scope")
        final_claims = authority.get("final_command_claims") if isinstance(authority, Mapping) else None
        if (
            not isinstance(authority, Mapping)
            or str(authority.get("selection_pending_id") or "") != pending.pending_id
            or str(authority.get("candidate_stable_id") or "") != candidate.stable_id
            or int(authority.get("candidate_version", -1)) != candidate.version
            or str(authority.get("original_source_digest") or "")
            != snapshot.original_source_digest
            or not isinstance(final_claims, Mapping)
            or str(final_claims.get("bound_payload_sha256") or "")
            != snapshot.bound_payload_sha256
        ):
            raise ValueError("selection admission ticket claims changed")
        evidence = dict(selection_evidence or {})
        if (
            str(evidence.get("source_message_id") or "")
            != str(ticket.get("source_message_id") or "")
            or str(evidence.get("segment_id") or "") != str(ticket.get("segment_id") or "")
            or str(evidence.get("text_sha256") or "")
            != str(ticket.get("segment_text_sha256") or "")
            or int(evidence.get("start_offset", -1))
            != int(ticket.get("segment_start_offset", -2))
            or int(evidence.get("end_offset", -1))
            != int(ticket.get("segment_end_offset", -2))
        ):
            raise ValueError("selection evidence does not match fresh admission ticket")
        payload["selection_evidence"] = evidence
    return TypedBusinessCommand(
        command_id=UUID(str(raw.get("command_id") or "")),
        decision_id=UUID(str(raw.get("decision_id") or "")),
        sub_decision_id=UUID(str(raw.get("sub_decision_id") or "")),
        command_type=str(raw.get("command_type") or ""),
        target_system=str(raw.get("target_system") or ""),
        entity_ids=tuple(str(item) for item in raw.get("entity_ids") or ()),
        payload=payload,
        execution_mode=str(raw.get("execution_mode") or "candidate"),  # type: ignore[arg-type]
        idempotency_key=str(raw.get("idempotency_key") or ""),
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id=str(ticket.get("action_id") or ""),
        admission_operation=str(ticket.get("operation") or ""),
    )


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
