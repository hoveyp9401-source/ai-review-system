from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


def compute_admission_claim_hashes(
    *,
    action_id: str,
    operation: str,
    segment_text_sha256: str,
    domain: str,
    object_ref: Mapping[str, Any],
    authority_scope: Mapping[str, Any],
    allowed_changed_fields: Sequence[str],
) -> tuple[str, str]:
    """Return the canonical fact and command digests bound into one ticket."""

    fact_claims_sha256 = _sha256_json(
        {
            "action_id": action_id,
            "operation": operation,
            "segment_text_sha256": segment_text_sha256,
            "authority_scope": dict(authority_scope),
            "allowed_changed_fields": list(allowed_changed_fields),
        }
    )
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
    return fact_claims_sha256, authorized_command_sha256


def admission_claim_hashes_match(ticket: Mapping[str, Any]) -> bool:
    object_ref = ticket.get("object_ref")
    authority_scope = ticket.get("authority_scope")
    allowed_changed_fields = ticket.get("allowed_changed_fields")
    if (
        not isinstance(object_ref, Mapping)
        or not isinstance(authority_scope, Mapping)
        or not isinstance(allowed_changed_fields, (list, tuple))
    ):
        return False
    fact_hash, command_hash = compute_admission_claim_hashes(
        action_id=str(ticket.get("action_id") or ""),
        operation=str(ticket.get("operation") or ""),
        segment_text_sha256=str(ticket.get("segment_text_sha256") or ""),
        domain=str(ticket.get("domain") or ""),
        object_ref=object_ref,
        authority_scope=authority_scope,
        allowed_changed_fields=tuple(str(value) for value in allowed_changed_fields),
    )
    return (
        fact_hash == str(ticket.get("fact_claims_sha256") or "")
        and command_hash == str(ticket.get("authorized_command_sha256") or "")
    )


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
