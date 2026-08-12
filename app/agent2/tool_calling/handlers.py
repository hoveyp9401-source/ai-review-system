from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
from typing import Any

from app.agent2.tool_calling.context import (
    TrustedClearPending,
    TrustedContext,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import ExecutionMode, ReceiptStatus, ToolReceipt


@dataclass(frozen=True)
class ShadowHandlerRequest:
    tool_name: str
    arguments: dict[str, Any]
    context: TrustedContext
    report: TrustedReportSnapshot | None
    source_report: TrustedReportSnapshot | None
    target_item_ids: tuple[str, ...]
    idempotency_key: str | None
    date_facts: dict[str, Any] = field(default_factory=dict)
    pending: TrustedClearPending | None = None
    pending_ttl_seconds: int | None = None


def simulate_query(request: ShadowHandlerRequest) -> ToolReceipt:
    report = request.report
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS,
        target=report,
        would_change=False,
        facts={
            "found": report is not None,
            "report": report.safe_snapshot() if report is not None else None,
        },
    )


def simulate_add(request: ShadowHandlerRequest) -> ToolReceipt:
    report = request.report
    existing = {
        (item.field, _normalized(item.content))
        for item in (report.items if report is not None else ())
    }
    proposed = []
    seen = set(existing)
    for item in request.arguments.get("items", ()):
        key = (str(item["field"]), _normalized(str(item["content"])))
        if key in seen:
            continue
        seen.add(key)
        proposed.append(item)
    would_change = bool(proposed)
    proposed_ids = tuple(
        _proposed_item_id(request.idempotency_key or request.tool_name, index)
        for index, _ in enumerate(proposed, start=1)
    )
    existing_empty_fields = set(
        report.acknowledged_empty_fields if report is not None else ()
    )
    proposed_empty_fields = tuple(
        field_name
        for field_name in request.arguments.get(
            "acknowledged_empty_fields", ()
        )
        if field_name not in existing_empty_fields
    )
    would_change = would_change or bool(proposed_empty_fields)
    would_change = would_change or bool(
        request.arguments.get("submit_after_write", False)
        and (report is None or report.status != "completed")
    )
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS if would_change else ReceiptStatus.NO_OP,
        target=report,
        would_change=would_change,
        affected_item_ids=proposed_ids,
        facts={
            "proposed_item_count": len(proposed),
            "duplicate_item_count": len(request.arguments.get("items", ())) - len(proposed),
            "proposed_items": proposed,
            "proposed_acknowledged_empty_fields": list(
                proposed_empty_fields
            ),
            "submit_after_write": bool(
                request.arguments.get("submit_after_write", False)
            ),
        },
    )


def simulate_edit(request: ShadowHandlerRequest) -> ToolReceipt:
    report = request.report
    replacement = str(request.arguments["replacement"])
    targets = [report.item(item_id) for item_id in request.target_item_ids] if report else []
    would_change = any(item is not None and item.content != replacement for item in targets)
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS if would_change else ReceiptStatus.NO_OP,
        target=report,
        would_change=would_change,
        affected_item_ids=request.target_item_ids if would_change else (),
        facts={"replacement": replacement, "target_item_ids": list(request.target_item_ids)},
    )


def simulate_delete(request: ShadowHandlerRequest) -> ToolReceipt:
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS,
        target=request.report,
        would_change=bool(request.target_item_ids),
        affected_item_ids=request.target_item_ids,
        facts={"target_item_ids": list(request.target_item_ids)},
    )


def simulate_move(request: ShadowHandlerRequest) -> ToolReceipt:
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS,
        target=request.report,
        would_change=bool(request.target_item_ids),
        affected_item_ids=request.target_item_ids,
        facts={
            "target_item_ids": list(request.target_item_ids),
            "source_field": request.arguments["source_field"],
            "target_field": request.arguments["target_field"],
        },
    )


def simulate_copy(request: ShadowHandlerRequest) -> ToolReceipt:
    source = request.source_report
    would_change = bool(source and source.items)
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS if would_change else ReceiptStatus.NO_OP,
        target=request.report,
        would_change=would_change,
        facts={
            "source_report": source.safe_snapshot() if source is not None else None,
            "target_report_date": (
                request.report.report_date.isoformat() if request.report is not None else None
            ),
            "report_confirmed": False,
            "confirmation_requires_later_trusted_version": True,
        },
    )


def simulate_correct_report_date(request: ShadowHandlerRequest) -> ToolReceipt:
    source = request.source_report
    target_date = request.date_facts.get("resolved_target_date")
    if source is None:
        raise RuntimeError("report date correction requires a trusted source report")
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS,
        target=source,
        would_change=True,
        facts={
            "source_report_date": source.report_date.isoformat(),
            "target_report_date": target_date,
            "acknowledged_empty_fields": list(
                request.arguments.get("acknowledged_empty_fields", ())
            ),
            "submit_after_correction": bool(
                request.arguments.get("submit_after_correction", False)
            ),
        },
    )


def simulate_complete_previous(request: ShadowHandlerRequest) -> ToolReceipt:
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS,
        target=request.report,
        would_change=bool(request.target_item_ids),
        affected_item_ids=request.target_item_ids,
        facts={
            "source_report_id": (
                str(request.source_report.report_id) if request.source_report is not None else None
            ),
            "completed_source_item_ids": list(request.target_item_ids),
        },
    )


def simulate_confirm(request: ShadowHandlerRequest) -> ToolReceipt:
    already_confirmed = bool(request.report and request.report.status == "completed")
    return _receipt(
        request,
        status=ReceiptStatus.NO_OP if already_confirmed else ReceiptStatus.SUCCESS,
        target=request.report,
        would_change=not already_confirmed,
        facts={"already_confirmed": already_confirmed},
    )


def simulate_request_clear(request: ShadowHandlerRequest) -> ToolReceipt:
    report = request.report
    if report is None or request.pending_ttl_seconds is None:
        raise RuntimeError("clear Pending proposal requires a bound report and Registry TTL")
    expires_at = request.context.now + timedelta(seconds=request.pending_ttl_seconds)
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS,
        target=report,
        would_change=True,
        facts={
            "pending_created": False,
            "pending_would_be_created": True,
            "report_would_be_cleared": False,
            "confirmation_required": True,
            "awaiting_user_confirmation": True,
            "confirmation_requires_separate_user_turn": True,
            "same_turn_confirmation_allowed": False,
            "target_date": report.report_date.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    )

def simulate_confirm_clear(request: ShadowHandlerRequest) -> ToolReceipt:
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS,
        target=request.report,
        would_change=True,
        facts={
            "pending_consumed": False,
            "pending_would_be_consumed": True,
            "report_would_be_cleared": True,
        },
    )


def _receipt(
    request: ShadowHandlerRequest,
    *,
    status: ReceiptStatus,
    target: TrustedReportSnapshot | None,
    would_change: bool,
    facts: dict[str, Any],
    affected_item_ids: tuple[str, ...] = (),
    server_evidence: dict[str, Any] | None = None,
) -> ToolReceipt:
    before_version = target.version if target is not None else None
    safe_facts = {
        "proposal_validated": True,
        "actual_write": False,
        "execution_mode": ExecutionMode.SHADOW_PROPOSAL,
        "would_change": would_change,
        "proposed_after_version": (
            before_version + 1 if before_version is not None and would_change else before_version
        ),
        **request.date_facts,
        **facts,
    }
    return ToolReceipt(
        status=status,
        tool_name=request.tool_name,
        changed=False,
        target_type="daily_report" if target is not None else "",
        target_id=str(target.report_id) if target is not None else "",
        before_version=before_version,
        after_version=before_version,
        affected_item_ids=affected_item_ids,
        safe_user_facts=safe_facts,
        server_evidence=server_evidence or {},
        execution_mode=ExecutionMode.SHADOW_PROPOSAL,
        would_change=would_change,
        idempotency_key=request.idempotency_key,
    )


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def _proposed_item_id(seed: str, index: int) -> str:
    digest = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).hexdigest()[:20]
    return f"shadow-proposed-item:{digest}"
