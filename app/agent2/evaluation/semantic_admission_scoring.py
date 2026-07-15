from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from app.agent2.json_immutability import freeze_json_value, thaw_json_value

from .semantic_admission_blind import (
    SemanticAdmissionActualArtifact,
    SemanticAdmissionBlindPack,
)


SEALED_LABEL_SCHEMA_VERSION = "agent2.semantic_admission_sealed_labels.v1"

_LABEL_FIELDS = frozenset(
    {
        "case_id",
        "expected_decisions",
        "expected_ticket_count",
        "expected_information_pending_count",
        "expected_selection_request_count",
        "annotation_class",
        "review_status",
    }
)
_EXPECTED_DECISION_FIELDS = frozenset(
    {"action_id", "domain", "operation", "status", "reason_code"}
)
_REVIEW_STATES = frozenset({"pending_human_review", "independently_adjudicated"})


@dataclass(frozen=True)
class SealedSemanticAdmissionLabels:
    input_pack_id: str
    input_pack_digest: str
    labels: tuple[Mapping[str, Any], ...]
    seal_hash: str
    schema_version: str = SEALED_LABEL_SCHEMA_VERSION

    @classmethod
    def seal(
        cls,
        *,
        input_pack: SemanticAdmissionBlindPack,
        labels: Sequence[Mapping[str, Any]],
    ) -> "SealedSemanticAdmissionLabels":
        normalized = _normalize_labels(labels)
        case_ids = {case.case_id for case in input_pack.cases}
        label_ids = {str(label["case_id"]) for label in normalized}
        if case_ids != label_ids:
            raise ValueError("sealed semantic admission labels must cover the blind pack exactly")
        body = {
            "schema_version": SEALED_LABEL_SCHEMA_VERSION,
            "input_pack_id": input_pack.pack_id,
            "input_pack_digest": input_pack.digest,
            "label_classification": "machine_candidate",
            "human_review_state": "pending_or_independent",
            "labels": [thaw_json_value(label) for label in normalized],
        }
        return cls(
            input_pack_id=input_pack.pack_id,
            input_pack_digest=input_pack.digest,
            labels=normalized,
            seal_hash=_json_digest(body),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SealedSemanticAdmissionLabels":
        allowed = frozenset(
            {
                "schema_version",
                "input_pack_id",
                "input_pack_digest",
                "label_classification",
                "human_review_state",
                "labels",
                "seal_hash",
            }
        )
        _require_closed_fields(payload, allowed, context="sealed semantic admission labels")
        if payload.get("schema_version") != SEALED_LABEL_SCHEMA_VERSION:
            raise ValueError("unsupported sealed semantic admission label schema")
        if payload.get("label_classification") != "machine_candidate":
            raise ValueError("sealed labels cannot claim independent Gold")
        if payload.get("human_review_state") != "pending_or_independent":
            raise ValueError("sealed label review state is invalid")
        raw_labels = payload.get("labels")
        if not isinstance(raw_labels, list) or not raw_labels:
            raise ValueError("sealed semantic admission labels require labels")
        normalized = _normalize_labels(raw_labels)
        body = {
            key: thaw_json_value(payload[key])
            for key in allowed
            if key != "seal_hash"
        }
        seal_hash = str(payload.get("seal_hash") or "")
        if not seal_hash or seal_hash != _json_digest(body):
            raise ValueError("sealed semantic admission label hash is invalid")
        return cls(
            input_pack_id=str(payload.get("input_pack_id") or ""),
            input_pack_digest=str(payload.get("input_pack_digest") or ""),
            labels=normalized,
            seal_hash=seal_hash,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "input_pack_id": self.input_pack_id,
            "input_pack_digest": self.input_pack_digest,
            "label_classification": "machine_candidate",
            "human_review_state": "pending_or_independent",
            "labels": [thaw_json_value(label) for label in self.labels],
            "seal_hash": self.seal_hash,
        }


def score_semantic_admission_actual(
    actual: SemanticAdmissionActualArtifact,
    sealed_labels: SealedSemanticAdmissionLabels,
) -> dict[str, Any]:
    """Score only after both independently hash-verified artifacts are loaded."""

    actual = SemanticAdmissionActualArtifact.from_mapping(actual.as_mapping())
    sealed_labels = SealedSemanticAdmissionLabels.from_mapping(
        sealed_labels.as_mapping()
    )
    if (
        actual.input_pack_id != sealed_labels.input_pack_id
        or actual.input_pack_digest != sealed_labels.input_pack_digest
    ):
        raise ValueError("actual artifact and sealed labels belong to different blind packs")
    actual_by_id = {str(row.get("case_id") or ""): row for row in actual.cases}
    labels_by_id = {
        str(label.get("case_id") or ""): label for label in sealed_labels.labels
    }
    if set(actual_by_id) != set(labels_by_id):
        raise ValueError("actual artifact and sealed labels have different case coverage")

    results: list[dict[str, Any]] = []
    mismatch_count = 0
    for case_id in sorted(actual_by_id):
        row = actual_by_id[case_id]
        label = labels_by_id[case_id]
        mismatches = _case_mismatches(row, label)
        mismatch_count += len(mismatches)
        results.append(
            {
                "case_id": case_id,
                "matched": not mismatches,
                "mismatches": mismatches,
                "review_status": label["review_status"],
                "annotation_class": label["annotation_class"],
            }
        )
    independent_metrics_available = all(
        label["review_status"] == "independently_adjudicated"
        for label in sealed_labels.labels
    )
    return {
        "schema_version": "agent2.semantic_admission_blind_score.v1",
        "input_pack_id": actual.input_pack_id,
        "input_pack_digest": actual.input_pack_digest,
        "actual_artifact_hash": actual.artifact_hash,
        "sealed_label_hash": sealed_labels.seal_hash,
        "label_count": len(sealed_labels.labels),
        "mismatch_count": mismatch_count,
        "matched_case_count": sum(1 for row in results if row["matched"]),
        "independent_metrics_available": independent_metrics_available,
        "acceptance_eligible": independent_metrics_available and mismatch_count == 0,
        "evidence_classification": "machine_candidate",
        "human_review_state": (
            "independently_adjudicated"
            if independent_metrics_available
            else "pending_human_review"
        ),
        "results": results,
    }


def _case_mismatches(
    actual: Mapping[str, Any], label: Mapping[str, Any]
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    actual_decision_rows = list(actual.get("decisions") or ())
    expected_decision_rows = list(label.get("expected_decisions") or ())
    if expected_decision_rows and all(
        str(row.get("action_id") or "").strip() for row in expected_decision_rows
    ):
        mismatches.extend(
            _action_bound_decision_mismatches(actual_decision_rows, expected_decision_rows)
        )
    else:
        mismatches.extend(
            _decision_signature_mismatches(actual_decision_rows, expected_decision_rows)
        )
    counts = {
        "ticket_count": len(actual.get("tickets") or ()),
        "information_pending_count": len(actual.get("information_pendings") or ()),
        "selection_request_count": len(actual.get("selection_requests") or ()),
    }
    expected_counts = {
        "ticket_count": int(label.get("expected_ticket_count", 0)),
        "information_pending_count": int(
            label.get("expected_information_pending_count", 0)
        ),
        "selection_request_count": int(
            label.get("expected_selection_request_count", 0)
        ),
    }
    for field_name, expected in expected_counts.items():
        if counts[field_name] != expected:
            mismatches.append(
                {
                    "field": field_name,
                    "expected": expected,
                    "actual": counts[field_name],
                }
            )
    return mismatches


def _action_bound_decision_mismatches(
    actual_rows: list[Mapping[str, Any]], expected_rows: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    actual = {str(row.get("action_id") or ""): row for row in actual_rows}
    expected = {str(row.get("action_id") or ""): row for row in expected_rows}
    result: list[dict[str, Any]] = []
    if set(actual) != set(expected):
        result.append(
            {
                "field": "decision_action_ids",
                "expected": sorted(expected),
                "actual": sorted(actual),
            }
        )
    for action_id in sorted(set(actual) & set(expected)):
        for field_name in ("domain", "operation", "status", "reason_code"):
            if field_name not in expected[action_id]:
                continue
            actual_value = _actual_decision_value(actual[action_id], field_name)
            if actual_value != expected[action_id].get(field_name):
                result.append(
                    {
                        "field": f"decision.{action_id}.{field_name}",
                        "expected": expected[action_id].get(field_name),
                        "actual": actual_value,
                    }
                )
    return result


def _decision_signature_mismatches(
    actual_rows: list[Mapping[str, Any]], expected_rows: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    unmatched_actual = list(actual_rows)
    missing: list[dict[str, Any]] = []
    for expected in expected_rows:
        match_index = next(
            (
                index
                for index, actual in enumerate(unmatched_actual)
                if all(
                    _actual_decision_value(actual, field_name) == expected.get(field_name)
                    for field_name in ("domain", "operation", "status", "reason_code")
                    if field_name in expected
                )
            ),
            None,
        )
        if match_index is None:
            missing.append(dict(expected))
        else:
            unmatched_actual.pop(match_index)
    if not missing and not unmatched_actual:
        return []
    return [
        {
            "field": "decision_signatures",
            "missing_expected": missing,
            "unexpected_actual": [
                {
                    "domain": row.get("domain"),
                    "operation": row.get("operation"),
                    "status": _actual_decision_value(row, "status"),
                    "reason_code": row.get("reason_code"),
                }
                for row in unmatched_actual
            ],
        }
    ]


def _actual_decision_value(row: Mapping[str, Any], field_name: str) -> Any:
    return row.get("verdict") if field_name == "status" else row.get(field_name)


def _normalize_labels(
    labels: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    if not labels:
        raise ValueError("sealed semantic admission labels require labels")
    normalized: list[Mapping[str, Any]] = []
    for index, raw in enumerate(labels):
        if not isinstance(raw, Mapping):
            raise ValueError(f"sealed label {index} must be an object")
        _require_closed_fields(raw, _LABEL_FIELDS, context=f"sealed label {index}")
        case_id = str(raw.get("case_id") or "").strip()
        decisions_raw = raw.get("expected_decisions")
        if not case_id or not isinstance(decisions_raw, list):
            raise ValueError("sealed label requires case_id and expected_decisions")
        decisions: list[dict[str, Any]] = []
        for decision_index, decision in enumerate(decisions_raw):
            if not isinstance(decision, Mapping):
                raise ValueError("sealed expected decision must be an object")
            _require_closed_fields(
                decision,
                _EXPECTED_DECISION_FIELDS,
                context=f"sealed label {index} decision {decision_index}",
            )
            required = ("domain", "operation", "status")
            if any(not str(decision.get(field) or "").strip() for field in required):
                raise ValueError("sealed expected decision is incomplete")
            decisions.append({str(key): thaw_json_value(value) for key, value in decision.items()})
        bound = [bool(str(item.get("action_id") or "").strip()) for item in decisions]
        if any(bound) and not all(bound):
            raise ValueError("sealed decision labels cannot mix bound and unbound action ids")
        annotation_class = str(raw.get("annotation_class") or "").strip()
        review_status = str(raw.get("review_status") or "").strip()
        if annotation_class != "machine_candidate":
            raise ValueError("labels are machine candidates until independently reviewed")
        if review_status not in _REVIEW_STATES:
            raise ValueError("sealed label review_status is invalid")
        normalized.append(
            freeze_json_value(
                {
                    "case_id": case_id,
                    "expected_decisions": decisions,
                    "expected_ticket_count": _nonnegative_int(
                        raw.get("expected_ticket_count"), "expected_ticket_count"
                    ),
                    "expected_information_pending_count": _nonnegative_int(
                        raw.get("expected_information_pending_count"),
                        "expected_information_pending_count",
                    ),
                    "expected_selection_request_count": _nonnegative_int(
                        raw.get("expected_selection_request_count"),
                        "expected_selection_request_count",
                    ),
                    "annotation_class": annotation_class,
                    "review_status": review_status,
                },
                path=f"sealed labels[{index}]",
            )
        )
    if len({str(label["case_id"]) for label in normalized}) != len(normalized):
        raise ValueError("sealed labels contain duplicate case ids")
    return tuple(normalized)


def _nonnegative_int(value: Any, field_name: str) -> int:
    result = int(value or 0)
    if result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _require_closed_fields(
    payload: Mapping[str, Any], allowed: frozenset[str], *, context: str
) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{context} contains unknown fields: {sorted(unknown)}")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
