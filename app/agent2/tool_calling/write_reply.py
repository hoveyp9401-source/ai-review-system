from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent2.tool_calling.contracts import ReceiptStatus, ToolReceipt

WriteOperationOutcome = Literal[
    "changed",
    "partial",
    "no_change",
    "not_executed",
    "needs_clarification",
]


class WriteReplyEnvelope(BaseModel):
    """Model-authored wording with server-checkable execution claims."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reply: str = Field(min_length=1, max_length=8000)
    actual_write: bool
    operation_outcome: WriteOperationOutcome


def expected_write_outcome(
    receipts: tuple[ToolReceipt, ...],
) -> WriteOperationOutcome:
    from app.agent2.tool_calling.registry import TOOL_REGISTRY

    write_receipts = tuple(
        receipt
        for receipt in receipts
        if (
            (definition := TOOL_REGISTRY.get(receipt.tool_name))
            is not None
            and definition.read_or_write == "write"
        )
    )
    has_changed = any(receipt.changed for receipt in write_receipts)
    has_non_success = any(
        receipt.status in {ReceiptStatus.BLOCKED, ReceiptStatus.FAILED}
        for receipt in write_receipts
    ) or any(
        receipt.status == ReceiptStatus.CLARIFICATION_REQUIRED
        for receipt in write_receipts
    )
    if has_changed and has_non_success:
        return "partial"
    if any(
        receipt.status in {ReceiptStatus.BLOCKED, ReceiptStatus.FAILED}
        for receipt in write_receipts
    ):
        return "not_executed"
    if any(
        receipt.status == ReceiptStatus.CLARIFICATION_REQUIRED
        for receipt in write_receipts
    ):
        return "needs_clarification"
    if has_changed:
        return "changed"
    return "no_change"


def model_safe_user_facts(receipt: ToolReceipt) -> dict[str, Any]:
    """Expose business facts to the model while retaining internal codes in audit."""

    facts = {
        key: value
        for key, value in receipt.safe_user_facts.items()
        if key
        not in {
            "error_code",
            "proposal_status",
            "execution_mode",
        }
    }
    facts["actual_write"] = bool(receipt.changed)
    facts["operation_outcome"] = expected_write_outcome((receipt,))
    return facts


def write_reply_protocol(
    receipts: tuple[ToolReceipt, ...],
) -> dict[str, Any]:
    return {
        "format": "json_object",
        "required_fields": {
            "reply": "natural user-facing text based only on safe_user_facts",
            "actual_write": "boolean",
            "operation_outcome": (
                "changed | partial | no_change | not_executed | "
                "needs_clarification"
            ),
        },
        "expected_actual_write": any(
            receipt.changed for receipt in receipts
        ),
        "expected_operation_outcome": expected_write_outcome(receipts),
        "rules": [
            "Return one JSON object and nothing else.",
            "Compose reply naturally; do not expose internal codes or identifiers.",
            (
                "Copy expected_actual_write and expected_operation_outcome exactly; "
                "these are server-computed fields, not values for the model to infer."
            ),
            (
                "A no-op alongside a successful change is still the server-provided "
                "aggregate outcome, not automatically partial."
            ),
            "Do not claim a write unless expected_actual_write is true.",
            "For partial, state separately what succeeded and what did not.",
            "Do not call another tool in this user turn.",
        ],
    }


def validate_write_reply(
    content: str,
    receipts: tuple[ToolReceipt, ...],
) -> tuple[WriteReplyEnvelope | None, tuple[str, ...]]:
    try:
        decoded = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, ("terminal response is not a JSON object",)
    try:
        envelope = WriteReplyEnvelope.model_validate(decoded)
    except ValidationError as exc:
        return None, tuple(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in exc.errors()
        )

    errors: list[str] = []
    expected_actual_write = any(receipt.changed for receipt in receipts)
    if envelope.actual_write is not expected_actual_write:
        errors.append("actual_write does not match server receipts")
    expected_outcome = expected_write_outcome(receipts)
    if envelope.operation_outcome != expected_outcome:
        errors.append("operation_outcome does not match server receipts")
    internal_codes = {
        str(receipt.error_code).strip()
        for receipt in receipts
        if receipt.error_code
    }
    if any(code and code in envelope.reply for code in internal_codes):
        errors.append("reply exposes an internal error code")
    return (envelope if not errors else None), tuple(errors)


def write_reply_retry_instruction(
    errors: tuple[str, ...],
    receipts: tuple[ToolReceipt, ...],
) -> str:
    required_exact_fields = {
        "actual_write": any(receipt.changed for receipt in receipts),
        "operation_outcome": expected_write_outcome(receipts),
    }
    return json.dumps(
        {
            "write_reply_retry": {
                "validation_errors": list(errors),
                "required_exact_fields": required_exact_fields,
                "instruction": (
                    "Return exactly one terminal JSON object. Copy "
                    "required_exact_fields unchanged into it. Compose only the "
                    "non-empty reply from the existing safe_user_facts. Do not "
                    "call tools. Do not return blank text or omit any field."
                ),
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
