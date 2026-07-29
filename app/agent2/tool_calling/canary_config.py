from __future__ import annotations

import hashlib


CANARY_MODEL_NAME = "deepseek-v4-pro"
CANARY_MODEL_PROVIDER = "DeepSeek"
CANARY_THINKING_ENABLED = False
CANARY_TIMEOUT_SECONDS = 60.0
CANARY_MAX_TOOL_LOOPS = 4
CANARY_MAX_REQUEST_ATTEMPTS = 2
CANARY_RETRY_BACKOFF_SECONDS = 0.25
CANARY_RECENT_MESSAGE_LIMIT = 12
CANARY_RECENT_OPERATION_LIMIT = 6


def canary_system_prompt() -> str:
    # Phase 1's sealed prompt remains the single source during preparation.
    from scripts.replay_agent2_tool_call_shadow import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def canary_prompt_sha256() -> str:
    return hashlib.sha256(
        canary_system_prompt().encode("utf-8")
    ).hexdigest()
