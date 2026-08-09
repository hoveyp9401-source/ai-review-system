from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    CurrentUserMessageEvidence,
    RememberPersonalMemoryArgs,
)


class CurrentTurnSourceEvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CurrentTurnSource:
    """Server-owned current-turn text and exact source-evidence checks."""

    messages: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.messages or len(self.messages) > 20:
            raise ValueError("current turn requires one to twenty user messages")
        if any(
            not isinstance(message, str) or not message.strip()
            for message in self.messages
        ):
            raise ValueError("current user messages must be non-empty strings")

    @property
    def canonical_text(self) -> str:
        return json.dumps(
            {
                "ordered_user_messages": [
                    {"sequence": index, "content": value}
                    for index, value in enumerate(self.messages, start=1)
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest()

    def validate_tool_arguments(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        if tool_name == "add_daily_items":
            typed = AddDailyItemsArgs.model_validate(arguments)
            for item in typed.items:
                self._validate_evidence(item.source_evidence)
            for item in typed.empty_field_evidence:
                self._validate_evidence(item.source_evidence)
            return
        if tool_name != "remember_personal_memory":
            return
        typed_memory = RememberPersonalMemoryArgs.model_validate(arguments)
        evidence = typed_memory.source_evidence
        source_message = self._validate_evidence(evidence)
        value = typed_memory.value.model_dump(mode="json")
        grounded_value = (
            value.get("name")
            if typed_memory.memory_key == "assistant.preferred_name"
            else (
                value.get("salutation")
                if typed_memory.memory_key
                == "response.preferred_salutation"
                else None
            )
        )
        if (
            isinstance(grounded_value, str)
            and grounded_value not in source_message
        ):
            raise CurrentTurnSourceEvidenceError(
                "MEMORY_VALUE_NOT_GROUNDED"
            )

    def _validate_evidence(
        self,
        evidence: CurrentUserMessageEvidence,
    ) -> str:
        index = evidence.source_message_index - 1
        if index < 0 or index >= len(self.messages):
            raise CurrentTurnSourceEvidenceError(
                "CURRENT_MESSAGE_EVIDENCE_MISMATCH"
            )
        return self.messages[index]
