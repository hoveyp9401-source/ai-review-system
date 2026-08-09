from __future__ import annotations

import hashlib
import json
from typing import Any


def build_write_idempotency_key(
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    source_message_id: str,
    tool_call_id: str,
    tool_name: str,
    canonical_arguments: dict[str, Any],
    target_object: str,
    expected_version: int | None,
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "source_message_id": source_message_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "canonical_arguments": canonical_arguments,
        "target_object": target_object,
        "expected_version": expected_version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"agent2-tool-call-v1:{hashlib.sha256(encoded).hexdigest()}"
