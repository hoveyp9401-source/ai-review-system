from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent2.tool_calling.context import (
        TrustedDailyWriteRetryCandidate,
    )

DAILY_RETRY_EVIDENCE_SCHEMA = "agent2.daily_write_retry_candidate.v1"
RECOVERABLE_DAILY_SOURCE_ERROR_CODES = frozenset(
    {
        "CURRENT_MESSAGE_EVIDENCE_MISMATCH",
        "DAILY_ITEM_CONTENT_NOT_GROUNDED",
        "DAILY_ITEM_EXACT_QUOTE_MISMATCH",
        "DAILY_ITEM_SOURCE_SPAN_AMBIGUOUS",
        "DAILY_ITEM_SOURCE_SPAN_OVERLAP",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


def daily_retry_candidate_id(
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    origin_source_message_id: str,
    source_bundle_sha256: str,
    target_report_date: str,
    target_state_sha256: str,
) -> str:
    payload = {
        "schema_version": DAILY_RETRY_EVIDENCE_SCHEMA,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "origin_source_message_id": origin_source_message_id,
        "source_bundle_sha256": source_bundle_sha256,
        "target_report_date": target_report_date,
        "target_state_sha256": target_state_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validated_daily_retry_evidence(
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(candidate, Mapping):
        return None
    candidate_id = str(candidate.get("candidate_id") or "")
    source_bundle_sha256 = str(
        candidate.get("source_bundle_sha256") or ""
    )
    target_report_date = str(
        candidate.get("target_report_date") or ""
    )
    target_state_sha256 = str(
        candidate.get("target_state_sha256") or ""
    )
    target_was_absent = candidate.get("target_was_absent")
    target_version = candidate.get("target_version")
    source_message_count = candidate.get("source_message_count")
    failed_local_date = str(candidate.get("failed_local_date") or "")
    retry_chain_depth = candidate.get("retry_chain_depth")
    retry_of_candidate_id = str(
        candidate.get("retry_of_candidate_id") or ""
    )
    if (
        candidate.get("schema_version") != DAILY_RETRY_EVIDENCE_SCHEMA
        or candidate.get("block_stage") != "source_binding"
        or candidate.get("retry_class")
        != "source_binding_recoverable"
        or _SHA256.fullmatch(candidate_id) is None
        or _SHA256.fullmatch(source_bundle_sha256) is None
        or _SHA256.fullmatch(target_state_sha256) is None
        or not isinstance(target_was_absent, bool)
        or not isinstance(source_message_count, int)
        or isinstance(source_message_count, bool)
        or source_message_count != 1
        or not isinstance(retry_chain_depth, int)
        or isinstance(retry_chain_depth, bool)
        or not 0 <= retry_chain_depth <= 3
        or (
            retry_of_candidate_id
            and retry_of_candidate_id != candidate_id
        )
    ):
        return None
    try:
        date.fromisoformat(target_report_date)
        date.fromisoformat(failed_local_date)
    except ValueError:
        return None
    if target_was_absent:
        if target_version is not None:
            return None
    elif (
        not isinstance(target_version, int)
        or isinstance(target_version, bool)
        or target_version < 0
    ):
        return None
    return {
        "schema_version": DAILY_RETRY_EVIDENCE_SCHEMA,
        "candidate_id": candidate_id,
        "block_stage": "source_binding",
        "retry_class": "source_binding_recoverable",
        "source_bundle_sha256": source_bundle_sha256,
        "source_message_count": 1,
        "target_report_date": target_report_date,
        "target_was_absent": target_was_absent,
        "target_version": target_version,
        "target_state_sha256": target_state_sha256,
        "failed_local_date": failed_local_date,
        "retry_chain_depth": retry_chain_depth,
        "retry_of_candidate_id": retry_of_candidate_id,
    }


def continued_daily_retry_evidence(
    candidate: TrustedDailyWriteRetryCandidate,
) -> dict[str, Any] | None:
    """Project one selected, zero-write retry for one more attempt."""

    if candidate.retry_chain_depth >= 3:
        return None
    return {
        "schema_version": DAILY_RETRY_EVIDENCE_SCHEMA,
        "candidate_id": candidate.candidate_id,
        "block_stage": "source_binding",
        "retry_class": "source_binding_recoverable",
        "source_bundle_sha256": candidate.source_bundle_sha256,
        "source_message_count": len(candidate.source_messages),
        "target_report_date": candidate.target_date.isoformat(),
        "target_was_absent": candidate.target_was_absent,
        "target_version": candidate.target_version,
        "target_state_sha256": candidate.target_state_sha256,
        "failed_local_date": candidate.failed_local_date.isoformat(),
        "retry_chain_depth": candidate.retry_chain_depth + 1,
        "retry_of_candidate_id": candidate.candidate_id,
    }
