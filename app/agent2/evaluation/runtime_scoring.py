from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from app.agent2.json_immutability import freeze_json_value, thaw_json_value
from app.agent2.runtime.blind import BlindActualArtifact, BlindInputPack


SEALED_LABEL_SCHEMA_VERSION = "agent2.runtime_sealed_labels.v1"
_LABEL_FIELDS = frozenset(
    {
        "case_id",
        "turn_id",
        "expected_current_goal",
        "expected_entities",
        "expected_segment_boundaries",
        "expected_domain_ownership",
        "expected_action_class",
        "expected_executable",
        "expected_clarification_requirement",
        "expected_write_intent",
        "expected_command_type",
        "expected_no_write_reason",
        "risk_annotation",
        "provenance",
        "confidence",
        "independent_review_status",
        "adjudication",
    }
)


@dataclass(frozen=True)
class SealedLabelStore:
    input_pack_id: str
    input_pack_digest: str
    labels: tuple[Mapping[str, Any], ...]
    seal_hash: str
    schema_version: str = SEALED_LABEL_SCHEMA_VERSION

    @classmethod
    def seal(
        cls,
        *,
        input_pack: BlindInputPack,
        labels: Sequence[Mapping[str, Any]],
    ) -> "SealedLabelStore":
        normalized = _normalize_labels(labels)
        body = {
            "schema_version": SEALED_LABEL_SCHEMA_VERSION,
            "input_pack_id": input_pack.pack_id,
            "input_pack_digest": input_pack.digest,
            "labels": [thaw_json_value(label) for label in normalized],
        }
        return cls(
            input_pack_id=input_pack.pack_id,
            input_pack_digest=input_pack.digest,
            labels=normalized,
            seal_hash=_json_digest(body),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SealedLabelStore":
        allowed = {
            "schema_version",
            "input_pack_id",
            "input_pack_digest",
            "labels",
            "seal_hash",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"sealed label store contains unknown fields: {sorted(unknown)}")
        if payload.get("schema_version") != SEALED_LABEL_SCHEMA_VERSION:
            raise ValueError("unsupported sealed label schema")
        raw_labels = payload.get("labels")
        if not isinstance(raw_labels, list):
            raise ValueError("sealed label store labels must be an array")
        labels = _normalize_labels(raw_labels)
        body = {
            "schema_version": SEALED_LABEL_SCHEMA_VERSION,
            "input_pack_id": str(payload.get("input_pack_id") or ""),
            "input_pack_digest": str(payload.get("input_pack_digest") or ""),
            "labels": [thaw_json_value(label) for label in labels],
        }
        seal_hash = str(payload.get("seal_hash") or "")
        if not seal_hash or seal_hash != _json_digest(body):
            raise ValueError("sealed label store hash is invalid")
        return cls(
            input_pack_id=body["input_pack_id"],
            input_pack_digest=body["input_pack_digest"],
            labels=labels,
            seal_hash=seal_hash,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "input_pack_id": self.input_pack_id,
            "input_pack_digest": self.input_pack_digest,
            "labels": [thaw_json_value(label) for label in self.labels],
            "seal_hash": self.seal_hash,
        }


def score_actual_artifact(
    actual: BlindActualArtifact,
    labels: SealedLabelStore,
) -> dict[str, Any]:
    """Score a completed Actual Artifact after the Runtime process has ended."""

    actual = BlindActualArtifact.from_mapping(actual.as_mapping())
    labels = SealedLabelStore.from_mapping(labels.as_mapping())
    if actual.input_pack_id != labels.input_pack_id:
        raise ValueError("actual artifact and sealed labels have different pack IDs")
    if actual.input_pack_digest != labels.input_pack_digest:
        raise ValueError("actual artifact and sealed labels have different input digests")
    actual_turns = {
        (str(case.get("case_id") or ""), str(turn.get("turn_id") or "")): turn
        for case in actual.cases
        for turn in case.get("turns") or []
    }
    scored: list[dict[str, Any]] = []
    for label in labels.labels:
        key = (str(label["case_id"]), str(label["turn_id"]))
        turn = actual_turns.get(key)
        if turn is None:
            raise ValueError(f"sealed label has no Runtime actual: {key[0]}/{key[1]}")
        field_matches, excluded_fields = _field_matches(turn, label)
        scored.append(
            {
                "case_id": key[0],
                "turn_id": key[1],
                "matched": all(field_matches.values()),
                "field_matches": field_matches,
                "excluded_fields": excluded_fields,
                "independent_review_status": label["independent_review_status"],
            }
        )
    reviewed = all(
        str(label.get("independent_review_status") or "") == "human_approved"
        for label in labels.labels
    )
    return {
        "schema_version": "agent2.runtime_blind_score.v1",
        "actual_artifact_hash": actual.artifact_hash,
        "sealed_label_hash": labels.seal_hash,
        "input_pack_digest": actual.input_pack_digest,
        "label_count": len(labels.labels),
        "mismatch_count": sum(not row["matched"] for row in scored),
        "independent_metrics_available": bool(scored) and reviewed,
        "scored_turns": scored,
    }


def _normalize_labels(
    labels: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    normalized: list[Mapping[str, Any]] = []
    keys: set[tuple[str, str]] = set()
    for index, raw in enumerate(labels, start=1):
        unknown = set(raw) - _LABEL_FIELDS
        if unknown:
            raise ValueError(f"sealed label {index} contains unknown fields: {sorted(unknown)}")
        case_id = str(raw.get("case_id") or "").strip()
        turn_id = str(raw.get("turn_id") or "").strip()
        review_status = str(raw.get("independent_review_status") or "").strip()
        if not case_id or not turn_id or not review_status:
            raise ValueError("sealed labels require case, turn, and independent review status")
        key = (case_id, turn_id)
        if key in keys:
            raise ValueError(f"sealed labels contain duplicate key: {case_id}/{turn_id}")
        keys.add(key)
        normalized.append(
            freeze_json_value(
                {**thaw_json_value(raw), "case_id": case_id, "turn_id": turn_id},
                path=f"sealed label {index}",
            )
        )
    if not normalized:
        raise ValueError("sealed label store requires at least one label")
    return tuple(normalized)


def _field_matches(
    actual: Mapping[str, Any],
    label: Mapping[str, Any],
) -> tuple[dict[str, bool], list[str]]:
    comparisons: dict[str, bool] = {}
    excluded_fields: list[str] = []
    mapping = {
        "expected_write_intent": "write_intent",
        "expected_clarification_requirement": "clarification_requirement",
    }
    for expected_key, actual_key in mapping.items():
        if expected_key in label:
            comparisons[expected_key] = bool(label[expected_key]) == bool(actual.get(actual_key))
    provenance = label.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    action_coverage = str(provenance.get("action_coverage") or "exact")
    if action_coverage in {"inconsistent", "coarse_legacy_command", "none"}:
        for field_name in ("expected_action_class", "expected_command_type"):
            if field_name in label:
                excluded_fields.append(field_name)
    elif "expected_action_class" in label:
        expected_actions = list(label["expected_action_class"] or [])
        actual_actions = list(actual.get("action_class") or [])
        comparisons["expected_action_class"] = (
            set(expected_actions).issubset(actual_actions)
            if action_coverage == "machine_candidate"
            else expected_actions == actual_actions
        )
    if action_coverage not in {"inconsistent", "coarse_legacy_command", "none"} and "expected_command_type" in label:
        actual_command_types = [
            str(command.get("command_type") or "")
            for command in actual.get("typed_commands") or []
            if isinstance(command, Mapping)
        ]
        expected_commands = list(label["expected_command_type"] or [])
        comparisons["expected_command_type"] = (
            set(expected_commands).issubset(actual_command_types)
            if action_coverage == "machine_candidate"
            else expected_commands == actual_command_types
        )
    return comparisons or {"label_has_scorable_field": False}, sorted(excluded_fields)


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
