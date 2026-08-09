from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from typing import Any, Mapping

from app.agent2.tool_calling.registry import validate_tool_arguments


BLIND_INPUT_SCHEMA = "agent2.tool_call_shadow_blind_input.v1"
SEALED_LABEL_SCHEMA = "agent2.tool_call_shadow_sealed_labels.v1"


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class BlindReplayPack:
    pack_id: str
    cases: tuple[Mapping[str, Any], ...]
    digest: str
    schema_version: str = BLIND_INPUT_SCHEMA

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BlindReplayPack":
        _require_keys(
            payload,
            {"schema_version", "pack_id", "cases", "digest"},
            "blind replay pack",
        )
        if payload.get("schema_version") != BLIND_INPUT_SCHEMA:
            raise ValueError("unsupported blind replay pack schema")
        pack_id = _required_text(payload.get("pack_id"), "pack_id")
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("blind replay pack requires cases")
        cases: list[Mapping[str, Any]] = []
        case_ids: set[str] = set()
        for index, raw in enumerate(raw_cases, start=1):
            if not isinstance(raw, Mapping):
                raise ValueError(f"blind replay case {index} must be an object")
            _require_keys(
                raw,
                {"case_id", "user_text", "trusted_context", "date_resolutions"},
                f"blind replay case {index}",
            )
            case_id = _required_text(raw.get("case_id"), "case_id")
            if case_id in case_ids:
                raise ValueError(f"duplicate blind replay case: {case_id}")
            case_ids.add(case_id)
            _required_text(raw.get("user_text"), "user_text")
            if not isinstance(raw.get("trusted_context"), Mapping):
                raise ValueError("trusted_context must be an object")
            if not isinstance(raw.get("date_resolutions"), list):
                raise ValueError("date_resolutions must be an array")
            cases.append(_json_copy(raw))
        body = {
            "schema_version": BLIND_INPUT_SCHEMA,
            "pack_id": pack_id,
            "cases": [_json_copy(item) for item in cases],
        }
        digest = _required_text(payload.get("digest"), "digest")
        if digest != canonical_digest(body):
            raise ValueError("blind replay pack digest is invalid")
        return cls(pack_id=pack_id, cases=tuple(cases), digest=digest)


@dataclass(frozen=True)
class SealedReplayLabels:
    input_pack_id: str
    input_pack_digest: str
    labels: tuple[Mapping[str, Any], ...]
    seal_hash: str
    schema_version: str = SEALED_LABEL_SCHEMA

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SealedReplayLabels":
        _require_keys(
            payload,
            {
                "schema_version",
                "input_pack_id",
                "input_pack_digest",
                "labels",
                "seal_hash",
            },
            "sealed replay labels",
        )
        if payload.get("schema_version") != SEALED_LABEL_SCHEMA:
            raise ValueError("unsupported sealed replay label schema")
        body = {key: _json_copy(value) for key, value in payload.items() if key != "seal_hash"}
        seal_hash = _required_text(payload.get("seal_hash"), "seal_hash")
        if seal_hash != canonical_digest(body):
            raise ValueError("sealed replay label hash is invalid")
        input_pack_id = _required_text(payload.get("input_pack_id"), "input_pack_id")
        input_pack_digest = _required_text(
            payload.get("input_pack_digest"),
            "input_pack_digest",
        )
        raw_labels = payload.get("labels")
        if not isinstance(raw_labels, list) or not raw_labels:
            raise ValueError("sealed replay labels require labels")
        labels: list[Mapping[str, Any]] = []
        case_ids: set[str] = set()
        for index, raw in enumerate(raw_labels, start=1):
            labels.append(_validated_label(raw, index=index, case_ids=case_ids))
        return cls(input_pack_id, input_pack_digest, tuple(labels), seal_hash)


def _validated_label(
    raw: Any,
    *,
    index: int,
    case_ids: set[str],
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"sealed replay label {index} must be an object")
    allowed = {
        "case_id",
        "expected_outcome",
        "expected_tool_calls",
        "expected_resolved_dates",
        "expected_rejection_code",
        "independent_review_status",
        "annotation_source",
    }
    _require_keys(raw, allowed, f"sealed replay label {index}")
    case_id = _required_text(raw.get("case_id"), "case_id")
    if case_id in case_ids:
        raise ValueError(f"duplicate sealed replay label: {case_id}")
    case_ids.add(case_id)
    if raw.get("annotation_source") != "human":
        raise ValueError("sealed replay Gold must come from a human")
    if raw.get("independent_review_status") not in {"human_approved", "human_corrected"}:
        raise ValueError("sealed replay Gold requires human approval")
    outcome = raw.get("expected_outcome")
    if outcome not in {"tool_calls", "direct_answer", "clarification"}:
        raise ValueError("invalid expected outcome")
    raw_calls = raw.get("expected_tool_calls")
    if not isinstance(raw_calls, list):
        raise ValueError("expected_tool_calls must be an array")
    calls: list[dict[str, Any]] = []
    for call in raw_calls:
        if not isinstance(call, Mapping):
            raise ValueError("expected tool call must be an object")
        _require_keys(call, {"tool_name", "arguments"}, "expected tool call")
        name = _required_text(call.get("tool_name"), "tool_name")
        calls.append(
            {
                "tool_name": name,
                "arguments": validate_tool_arguments(name, call.get("arguments")),
            }
        )
    if outcome == "tool_calls" and not calls:
        raise ValueError("tool-call outcome requires expected calls")
    if outcome != "tool_calls" and calls:
        raise ValueError("non-tool outcome cannot contain expected calls")
    resolved_dates = raw.get("expected_resolved_dates")
    if not isinstance(resolved_dates, list):
        raise ValueError("expected_resolved_dates must be an array")
    for value in resolved_dates:
        date.fromisoformat(_required_text(value, "expected_resolved_date"))
    rejection = raw.get("expected_rejection_code")
    if rejection is not None and not isinstance(rejection, str):
        raise ValueError("expected_rejection_code must be text or null")
    return {
        **_json_copy(raw),
        "case_id": case_id,
        "expected_tool_calls": calls,
    }


def _require_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown or missing:
        raise ValueError(
            f"{label} fields are invalid; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
