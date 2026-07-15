from __future__ import annotations

import hashlib
from typing import Any


def canonical_dingtalk_idempotency_key(
    *,
    message_id: Any,
    user_id: Any,
    conversation_id: Any,
    text: Any,
    created_at: Any,
) -> str:
    """Return one business-consumption key independent of DingTalk transport.

    DingTalk can deliver the same logical message through HTTP callbacks and
    Stream mode. Transport names therefore must not participate in the key.
    The provider message id is authoritative when present; the bounded
    fingerprint is only a fallback for legacy payloads without that id.
    """

    provider_message_id = str(message_id or "").strip()
    if provider_message_id:
        return f"dingtalk:{provider_message_id}"
    seed = "|".join(
        [
            str(user_id or "").strip(),
            str(conversation_id or "").strip(),
            str(text or "").strip(),
            str(created_at or "").strip(),
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"dingtalk:sha256:{digest}"
