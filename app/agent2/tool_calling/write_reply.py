from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent2.tool_calling.contracts import (
    ReceiptStatus,
    ReportField,
    ToolReceipt,
)

WriteOperationOutcome = Literal[
    "changed",
    "partial",
    "no_change",
    "not_executed",
    "needs_clarification",
]
DailySectionState = Literal["filled", "acknowledged_empty", "missing"]
_DAILY_MISSING_LABELS_TOKEN = "{{daily_missing_section_labels}}"
_DAILY_MISSING_REPORT_SUMMARY_TOKEN = "{{daily_missing_report_summary}}"
_HISTORICAL_REPORT_LOCK_FACTS_TOKEN = "{{historical_report_lock_facts}}"
_DAILY_REPLY_STATE_FACT_KEYS = frozenset(
    {
        "section_states",
        "missing_sections",
        "missing_section_labels",
        "confirmation_available",
        "persisted_draft_available",
    }
)


class DailyReportSectionStates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    today_work: DailySectionState
    problems: DailySectionState
    tomorrow_plan: DailySectionState


class DailyReportReplyState(BaseModel):
    """Server-checkable claims that accompany Agent2's natural reply."""

    model_config = ConfigDict(extra="forbid")

    section_states: DailyReportSectionStates
    missing_sections: tuple[ReportField, ...]
    confirmation_available: bool
    persisted_draft_available: bool


class DatedDailyReportReplyState(DailyReportReplyState):
    """One report's facts when a turn contains more than one report date."""

    report_date: str = Field(min_length=10, max_length=10)
    missing_section_labels: tuple[str, ...]


class HistoricalReportLockReplyState(BaseModel):
    """Immutable cutoff facts that Agent2 must preserve in its reply."""

    model_config = ConfigDict(extra="forbid")

    report_date: str = Field(min_length=10, max_length=10)
    cutoff_local_time: str = Field(min_length=5, max_length=5)
    automatically_unlocks: Literal[False]
    allowed_actions: tuple[Literal["query_report_by_date"], ...]


class WriteReplyEnvelope(BaseModel):
    """Model-authored wording with server-checkable execution claims."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reply: str = Field(min_length=1, max_length=8000)
    actual_write: bool
    operation_outcome: WriteOperationOutcome
    daily_report_state: DailyReportReplyState | None = None
    daily_report_states: tuple[DatedDailyReportReplyState, ...] | None = None
    historical_report_lock_state: HistoricalReportLockReplyState | None = None


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
            "pre_execution_block_observation",
        }
    }
    report_snapshot = facts.get("report_snapshot")
    if isinstance(report_snapshot, dict):
        facts["report_snapshot"] = {
            key: value
            for key, value in report_snapshot.items()
            if key != "report_state_sha256"
        }
    facts["actual_write"] = bool(receipt.changed)
    facts["operation_outcome"] = expected_write_outcome((receipt,))
    return facts


def write_reply_protocol(
    receipts: tuple[ToolReceipt, ...],
) -> dict[str, Any]:
    clarification_options = _required_clarification_options(receipts)
    expected_daily_state = _expected_daily_report_state(receipts)
    expected_daily_states = _expected_daily_report_states(receipts)
    required_daily_reply_mode = _required_daily_reply_mode(receipts)
    missing_label_requirements = _daily_missing_label_requirements(receipts)
    expected_historical_lock_state = _expected_historical_report_lock_state(
        receipts
    )
    protocol = {
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
            (
                "When clarification_option_labels is non-empty, ask the user to "
                "choose among every label naturally and do not imply a write."
            ),
            "Do not call another tool in this user turn.",
        ],
        "clarification_option_labels": list(clarification_options),
    }
    if expected_daily_state is not None:
        protocol["required_fields"]["daily_report_state"] = (
            "copy expected_daily_report_state exactly"
        )
        protocol["expected_daily_report_state"] = (
            expected_daily_state.model_dump(mode="json")
        )
        protocol["rules"].append(
            "Keep the natural reply consistent with expected_daily_report_state: "
            "mention only its missing sections, do not call a missing section empty, "
            "do not invite confirmation when confirmation_available is false, and do "
            "not claim a saved draft when persisted_draft_available is false."
        )
        required_daily_labels = missing_label_requirements[0][1]
        protocol["required_daily_missing_labels"] = list(required_daily_labels)
        if required_daily_labels:
            protocol["daily_missing_labels_token"] = (
                _DAILY_MISSING_LABELS_TOKEN
            )
            protocol["rules"].append(
                "Put daily_missing_labels_token exactly once where the missing "
                "section names belong. Do not spell any daily section label "
                "elsewhere in reply; the server will insert only the verified labels."
            )
    elif expected_daily_states:
        protocol["required_fields"]["daily_report_states"] = (
            "copy expected_daily_report_states exactly in the same order"
        )
        protocol["expected_daily_report_states"] = [
            state.model_dump(mode="json") for state in expected_daily_states
        ]
        states_with_missing_labels = tuple(
            state for state in expected_daily_states if state.missing_section_labels
        )
        if states_with_missing_labels:
            protocol["daily_missing_report_summary"] = {
                "token": _DAILY_MISSING_REPORT_SUMMARY_TOKEN,
                "reports": [
                    {
                        "report_date": state.report_date,
                        "labels": list(state.missing_section_labels),
                    }
                    for state in states_with_missing_labels
                ],
            }
            protocol["rules"].append(
                "Keep each natural-language daily-report statement consistent with "
                "expected_daily_report_states. Put "
                "daily_missing_report_summary.token exactly once where the "
                "date-to-missing-section facts belong; the server will insert every "
                "verified date and its matching labels as one indivisible summary. "
                "Do not spell a report date or daily section label elsewhere in "
                "reply."
            )
    if required_daily_reply_mode is not None:
        protocol["required_daily_reply_mode"] = required_daily_reply_mode
        if required_daily_reply_mode == "completion_acknowledgement_only":
            protocol["rules"].append(
                "The daily reply mode is completion_acknowledgement_only: "
                "acknowledge the completed result, then end the reply. Do not ask "
                "a follow-up question, request confirmation, offer another report "
                "action, or ask whether anything else is needed."
            )
    if expected_historical_lock_state is not None:
        protocol["required_fields"]["reply"] = (
            "copy historical_report_lock_facts_token exactly"
        )
        protocol["required_fields"]["historical_report_lock_state"] = (
            "copy expected_historical_report_lock_state exactly"
        )
        protocol["historical_report_lock_facts_token"] = (
            _HISTORICAL_REPORT_LOCK_FACTS_TOKEN
        )
        protocol["expected_historical_report_lock_state"] = (
            expected_historical_lock_state.model_dump(mode="json")
        )
        protocol["rules"].append(
            "Set reply to historical_report_lock_facts_token exactly. Do not add "
            "dates, unlocking claims, submission claims, or any other factual "
            "wording around it; the server will render the verified facts."
        )
    return protocol


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
    expected_daily_state = _expected_daily_report_state(receipts)
    expected_daily_states = _expected_daily_report_states(receipts)
    if envelope.daily_report_state != expected_daily_state:
        errors.append("daily_report_state does not match server receipts")
    if envelope.daily_report_states != (expected_daily_states or None):
        errors.append("daily_report_states do not match server receipts")
    expected_historical_lock_state = _expected_historical_report_lock_state(
        receipts
    )
    if envelope.historical_report_lock_state != expected_historical_lock_state:
        errors.append(
            "historical_report_lock_state does not match server receipts"
        )
    if expected_historical_lock_state is not None:
        if envelope.reply != _HISTORICAL_REPORT_LOCK_FACTS_TOKEN:
            errors.append(
                "reply must use only the verified historical lock facts token"
            )
    elif _HISTORICAL_REPORT_LOCK_FACTS_TOKEN in envelope.reply:
        errors.append("reply contains an unexpected historical lock facts token")
    internal_codes = {
        str(receipt.error_code).strip()
        for receipt in receipts
        if receipt.error_code
    }
    if any(code and code in envelope.reply for code in internal_codes):
        errors.append("reply exposes an internal error code")
    required_options = _required_clarification_options(receipts)
    missing_options = tuple(
        label for label in required_options if label not in envelope.reply
    )
    if missing_options:
        errors.append(
            "reply omits required clarification options: "
            + ", ".join(missing_options)
        )
    missing_label_requirements = _daily_missing_label_requirements(receipts)
    required_tokens = (
        (_DAILY_MISSING_REPORT_SUMMARY_TOKEN,)
        if expected_daily_states
        and any(state.missing_section_labels for state in expected_daily_states)
        else tuple(
            token for token, labels in missing_label_requirements if labels
        )
    )
    for token in required_tokens:
        if envelope.reply.count(token) != 1:
            errors.append(
                f"reply must contain verified missing-section token {token} exactly once"
            )
    reply_without_tokens = envelope.reply
    for token in required_tokens:
        reply_without_tokens = reply_without_tokens.replace(token, "")
    if "{{daily_missing_" in reply_without_tokens:
        errors.append("reply contains an unexpected daily missing-section token")
    if required_tokens:
        expanded_labels = tuple(
            label
            for label in _all_daily_section_labels(receipts)
            if label in reply_without_tokens
        )
        if expanded_labels:
            errors.append(
                "reply expands daily section labels outside the verified token: "
                + ", ".join(expanded_labels)
            )
    return (envelope if not errors else None), tuple(errors)


def _required_clarification_options(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[str, ...]:
    options: list[str] = []
    for receipt in receipts:
        if receipt.status != ReceiptStatus.CLARIFICATION_REQUIRED:
            continue
        raw = receipt.safe_user_facts.get("clarification_option_labels", ())
        if not isinstance(raw, (list, tuple)):
            continue
        for value in raw:
            label = str(value).strip()
            if label and label not in options:
                options.append(label)
    return tuple(options)


def write_reply_retry_instruction(
    errors: tuple[str, ...],
    receipts: tuple[ToolReceipt, ...],
) -> str:
    required_exact_fields = {
        "actual_write": any(receipt.changed for receipt in receipts),
        "operation_outcome": expected_write_outcome(receipts),
    }
    expected_daily_state = _expected_daily_report_state(receipts)
    expected_daily_states = _expected_daily_report_states(receipts)
    required_daily_reply_mode = _required_daily_reply_mode(receipts)
    if expected_daily_state is not None:
        required_exact_fields["daily_report_state"] = (
            expected_daily_state.model_dump(mode="json")
        )
    elif expected_daily_states:
        required_exact_fields["daily_report_states"] = [
            state.model_dump(mode="json") for state in expected_daily_states
        ]
    expected_historical_lock_state = _expected_historical_report_lock_state(
        receipts
    )
    if expected_historical_lock_state is not None:
        required_exact_fields["reply"] = _HISTORICAL_REPORT_LOCK_FACTS_TOKEN
        required_exact_fields["historical_report_lock_state"] = (
            expected_historical_lock_state.model_dump(mode="json")
        )
    clarification_options = _required_clarification_options(receipts)
    missing_label_requirements = _daily_missing_label_requirements(receipts)
    return json.dumps(
        {
            "write_reply_retry": {
                "validation_errors": list(errors),
                "required_exact_fields": required_exact_fields,
                "clarification_option_labels": list(clarification_options),
                "daily_missing_label_requirements": (
                    [
                        {
                            "token": _DAILY_MISSING_REPORT_SUMMARY_TOKEN,
                            "reports": [
                                {
                                    "report_date": state.report_date,
                                    "labels": list(state.missing_section_labels),
                                }
                                for state in expected_daily_states
                                if state.missing_section_labels
                            ],
                        }
                    ]
                    if expected_daily_states
                    and any(
                        state.missing_section_labels
                        for state in expected_daily_states
                    )
                    else [
                        {"token": token, "labels": list(labels)}
                        for token, labels in missing_label_requirements
                        if labels
                    ]
                ),
                "required_daily_reply_mode": required_daily_reply_mode,
                "instruction": (
                    "Return exactly one terminal JSON object. Copy "
                    "required_exact_fields unchanged into it. Compose only the "
                    "non-empty reply from the existing safe_user_facts. If "
                    "clarification_option_labels is non-empty, naturally ask the "
                    "user to choose among every label. Include every "
                    "daily missing-section labels only through their exact "
                    "listed token placeholders. Follow required_daily_reply_mode; "
                    "completion_acknowledgement_only must end after acknowledging "
                    "completion, without a follow-up question or further action. "
                    "When historical_report_lock_state is required, set reply to "
                    "the required_exact_fields reply token with no surrounding "
                    "factual wording; the server renders the verified facts. Do not "
                    "call tools. Do not return blank text or omit any field."
                ),
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def write_reply_retry_messages(
    *,
    errors: tuple[str, ...],
    receipts: tuple[ToolReceipt, ...],
    retry_number: int,
) -> list[dict[str, str]]:
    """Build an isolated, receipt-bound retry for a failed write reply."""

    retry_contract = json.loads(write_reply_retry_instruction(errors, receipts))
    safe_receipts = [
        {
            "tool_name": receipt.tool_name,
            "status": receipt.status.value,
            "changed": bool(receipt.changed),
            "safe_user_facts": model_safe_user_facts(receipt),
        }
        for receipt in receipts
    ]
    return [
        {
            "role": "system",
            "content": (
                "You are an isolated Agent2 final-reply composer. Return exactly "
                "one JSON object and no other text. Do not call tools. Use only "
                "the supplied safe receipt facts. Copy every server-required "
                "field exactly. Write a concise natural reply without internal "
                "codes, identifiers, hashes, or unsupported claims."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "write_reply_composer": {
                        "retry_number": retry_number,
                        "safe_receipts": safe_receipts,
                        **retry_contract["write_reply_retry"],
                    }
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def _expected_daily_report_state(
    receipts: tuple[ToolReceipt, ...],
) -> DailyReportReplyState | None:
    state_receipts = _latest_daily_state_receipts(receipts)
    if len(state_receipts) != 1:
        return None
    return _daily_report_reply_state(state_receipts[0])


def _expected_historical_report_lock_state(
    receipts: tuple[ToolReceipt, ...],
) -> HistoricalReportLockReplyState | None:
    states: list[HistoricalReportLockReplyState] = []
    for receipt in receipts:
        raw_state = receipt.safe_user_facts.get("historical_report_lock")
        if raw_state is None:
            continue
        try:
            state = HistoricalReportLockReplyState.model_validate(raw_state)
        except ValidationError:
            continue
        if state not in states:
            states.append(state)
    return states[0] if len(states) == 1 else None


def _expected_daily_report_states(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[DatedDailyReportReplyState, ...]:
    state_receipts = _latest_daily_state_receipts(receipts)
    if len(state_receipts) <= 1:
        return ()
    states: list[DatedDailyReportReplyState] = []
    for receipt in state_receipts:
        facts = receipt.safe_user_facts
        report_date = str(facts.get("report_date") or "").strip()
        labels = tuple(str(value).strip() for value in facts["missing_section_labels"])
        states.append(
            DatedDailyReportReplyState(
                **_daily_report_reply_state(receipt).model_dump(),
                report_date=report_date,
                missing_section_labels=labels,
            )
        )
    return tuple(states)


def _latest_daily_state_receipts(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[ToolReceipt, ...]:
    latest: dict[str, ToolReceipt] = {}
    for receipt in receipts:
        facts = receipt.safe_user_facts
        if (
            receipt.target_type != "daily_report"
            or not _DAILY_REPLY_STATE_FACT_KEYS.issubset(facts)
        ):
            continue
        target_key = receipt.target_id or str(facts.get("report_date") or "")
        latest[target_key] = receipt
    return tuple(latest.values())


def _daily_report_reply_state(receipt: ToolReceipt) -> DailyReportReplyState:
    facts = receipt.safe_user_facts
    return DailyReportReplyState(
        section_states=DailyReportSectionStates.model_validate(facts["section_states"]),
        missing_sections=tuple(facts["missing_sections"]),
        confirmation_available=bool(facts["confirmation_available"]),
        persisted_draft_available=bool(facts["persisted_draft_available"]),
    )


def _required_daily_reply_mode(
    receipts: tuple[ToolReceipt, ...],
) -> str | None:
    state_receipts = _latest_daily_state_receipts(receipts)
    if not state_receipts:
        return None
    facts = tuple(receipt.safe_user_facts for receipt in state_receipts)
    all_receipts_are_daily_states = all(
        receipt.target_type == "daily_report"
        and _DAILY_REPLY_STATE_FACT_KEYS.issubset(receipt.safe_user_facts)
        and receipt.status not in {ReceiptStatus.BLOCKED, ReceiptStatus.FAILED}
        for receipt in receipts
    )
    if not all_receipts_are_daily_states:
        if any(item["missing_sections"] for item in facts) or any(
            receipt.status == ReceiptStatus.CLARIFICATION_REQUIRED
            for receipt in receipts
        ):
            return "mixed_status_with_clarification"
        return "mixed_status_update"
    if all(
        not bool(item["confirmation_available"])
        and not bool(item["persisted_draft_available"])
        for item in facts
    ):
        return "completion_acknowledgement_only"
    if any(item["missing_sections"] for item in facts):
        return "missing_sections_clarification"
    if any(bool(item["confirmation_available"]) for item in facts):
        return "confirmation_available"
    return "status_update_only"


def _daily_missing_label_requirements(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    state_receipts = _latest_daily_state_receipts(receipts)
    if len(state_receipts) != 1:
        return ()
    requirements: list[tuple[str, tuple[str, ...]]] = []
    for receipt in state_receipts:
        raw = receipt.safe_user_facts.get("missing_section_labels", ())
        labels = tuple(
            str(value).strip()
            for value in raw
            if str(value).strip()
        )
        requirements.append((_DAILY_MISSING_LABELS_TOKEN, labels))
    return tuple(requirements)


def _all_daily_section_labels(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[str, ...]:
    labels: list[str] = []
    for receipt in receipts:
        raw = receipt.safe_user_facts.get("section_labels", {})
        if not isinstance(raw, dict):
            continue
        for value in raw.values():
            label = str(value).strip()
            if label and label not in labels:
                labels.append(label)
    return tuple(labels)


def render_write_reply(
    envelope: WriteReplyEnvelope,
    receipts: tuple[ToolReceipt, ...],
) -> str:
    reply = envelope.reply
    historical_lock = _expected_historical_report_lock_state(receipts)
    if historical_lock is not None:
        return (
            f"{historical_lock.report_date} 的日报已在当日 "
            f"{historical_lock.cutoff_local_time} 后锁定，不会在之后自动解锁；"
            "目前只可查询，不能修改或移动。"
        )
    states = _expected_daily_report_states(receipts)
    if states:
        summary = "；".join(
            f"{state.report_date}：{'、'.join(state.missing_section_labels)}"
            for state in states
            if state.missing_section_labels
        )
        if summary:
            reply = reply.replace(_DAILY_MISSING_REPORT_SUMMARY_TOKEN, summary)
        return reply
    for token, labels in _daily_missing_label_requirements(receipts):
        if labels:
            reply = reply.replace(token, "、".join(labels))
    return reply
