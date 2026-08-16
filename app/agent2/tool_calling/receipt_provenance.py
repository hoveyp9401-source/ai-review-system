from __future__ import annotations

import hashlib
import json
from uuid import UUID


def principal_scope_sha256(
    *,
    tenant_id: str,
    user_id: str | UUID,
    conversation_id: str,
    source_message_id: str,
) -> str:
    """Bind a server receipt to the exact trusted ingress principal."""

    payload = {
        "tenant_id": tenant_id,
        "user_id": str(user_id),
        "conversation_id": conversation_id,
        "source_message_id": source_message_id,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
