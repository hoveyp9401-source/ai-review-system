from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.runtime import NativeToolCall


@dataclass(frozen=True)
class IncompleteConfirmReviewTarget:
    sequence: int
    original_call: NativeToolCall
    report_id: UUID
    expected_version: int
    missing_sections: tuple[str, ...]
    trusted_report_snapshot: dict[str, Any]


def incomplete_confirm_review_targets(
    calls: tuple[NativeToolCall, ...],
    *,
    context: TrustedContext,
) -> tuple[IncompleteConfirmReviewTarget, ...]:
    """Find incomplete trusted confirms without interpreting user language."""

    targets: list[IncompleteConfirmReviewTarget] = []
    for call in calls:
        if call.tool_name != "confirm_report":
            continue
        try:
            report_id = UUID(str(call.arguments.get("report_id")))
        except (TypeError, ValueError):
            continue
        report = context.report_by_id(report_id)
        if report is None or call.arguments.get("expected_version") != report.version:
            continue
        covered_sections = {item.field for item in report.items}
        covered_sections.update(report.acknowledged_empty_fields)
        missing_sections = tuple(
            section
            for section in ("today_work", "problems", "tomorrow_plan")
            if section not in covered_sections
        )
        if not missing_sections:
            continue
        targets.append(
            IncompleteConfirmReviewTarget(
                sequence=len(targets) + 1,
                original_call=call,
                report_id=report.report_id,
                expected_version=report.version,
                missing_sections=missing_sections,
                trusted_report_snapshot=report.safe_snapshot(),
            )
        )
    return tuple(targets)


def daily_incomplete_confirm_review_messages(
    *,
    ordered_messages: tuple[str, ...],
    targets: tuple[IncompleteConfirmReviewTarget, ...],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are an isolated Agent2 semantic reviewer for unexecuted "
                "confirm_report drafts whose trusted report snapshots are "
                "structurally missing one or more daily-report sections. The "
                "structural trigger does not decide what the user meant. Reread "
                "the exact current user messages independently. If a message "
                "supplies any new daily-report content or explicitly states that "
                "a report section has no content, replace that draft with one "
                "add_daily_items call bound to the exact same trusted report and "
                "version. Include only content or empty-section evidence supplied "
                "by the current messages; never reconstruct new content from the "
                "trusted snapshot or conversation history. Decide "
                "submit_after_write from the current user's meaning. If the "
                "current messages supply no new report content or explicit empty "
                "section and merely confirm the existing snapshot, preserve the "
                "confirm_report call with the exact same binding. This is semantic "
                "model judgment, never phrase, keyword, regular-expression, or "
                "program-branch interpretation. Return exactly one native tool "
                "call per draft in sequence order and no direct text. No draft has "
                "executed and no write has occurred."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ordered_current_user_messages": [
                        {"sequence": index, "content": content}
                        for index, content in enumerate(ordered_messages, start=1)
                    ],
                    "unexecuted_confirm_drafts": [
                        {
                            "sequence": target.sequence,
                            "tool_name": "confirm_report",
                            "arguments": {
                                "report_id": str(target.report_id),
                                "expected_version": target.expected_version,
                            },
                            "trusted_report_snapshot": (target.trusted_report_snapshot),
                            "structurally_missing_sections": list(
                                target.missing_sections
                            ),
                        }
                        for target in targets
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def validate_daily_incomplete_confirm_replacements(
    *,
    targets: tuple[IncompleteConfirmReviewTarget, ...],
    replacements: tuple[tuple[str, Mapping[str, Any]], ...],
    allowed_tool_names: frozenset[str],
) -> None:
    if len(replacements) != len(targets):
        raise ValueError("incomplete-confirm review must return one call per draft")
    for target, (tool_name, arguments) in zip(
        targets,
        replacements,
        strict=True,
    ):
        if tool_name not in {"confirm_report", "add_daily_items"}:
            raise ValueError("incomplete-confirm review returned an unsupported tool")
        if tool_name not in allowed_tool_names:
            raise ValueError("incomplete-confirm review returned a disallowed tool")
        if str(arguments.get("report_id")) != str(target.report_id):
            raise ValueError(
                "incomplete-confirm review changed the trusted report binding"
            )
        if arguments.get("expected_version") != target.expected_version:
            raise ValueError(
                "incomplete-confirm review changed the trusted report version"
            )
        if (
            tool_name == "add_daily_items"
            and arguments.get("date_selection") != "trusted_report"
        ):
            raise ValueError(
                "reviewed daily additions must use the trusted report binding"
            )
        if tool_name == "add_daily_items" and not (
            arguments.get("items") or arguments.get("acknowledged_empty_fields")
        ):
            raise ValueError(
                "a reviewed daily addition requires current-message content evidence"
            )


def attach_reviewed_omitted_sections(
    *,
    targets: tuple[IncompleteConfirmReviewTarget, ...],
    replacements: tuple[NativeToolCall, ...],
) -> tuple[NativeToolCall, ...]:
    """Attach server proof only after the independent review preserves confirm."""

    return tuple(
        NativeToolCall(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            arguments=(
                {
                    **call.arguments,
                    "reviewed_omitted_empty_fields": list(
                        target.missing_sections
                    ),
                }
                if call.tool_name == "confirm_report"
                else call.arguments
            ),
        )
        for target, call in zip(targets, replacements, strict=True)
    )
