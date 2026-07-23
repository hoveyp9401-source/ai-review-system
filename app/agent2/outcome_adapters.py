from __future__ import annotations

from typing import Any

from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)
from app.agent2.business.case_labels import case_stage_label, case_type_label


def periodic_execution_outcomes(
    results: tuple[Any, ...] | list[Any],
    *,
    source_turn_id: str,
) -> tuple[OperationOutcome, ...]:
    """Adapt persisted weekly/monthly executions into one receipt-backed outcome.

    Executor-authored prose is intentionally not part of this boundary.  The
    persisted receipt and the post-execution snapshot are the only facts used.
    """
    executions = tuple(results or ())
    if not executions:
        return ()
    last = executions[-1].execution
    command_types = tuple(str(item.execution.command.command_type) for item in executions)
    operation = _daily_operation(command_types)
    receipt_refs = tuple(
        OutcomeReceiptRef(
            receipt_id=str(item.receipt_id),
            receipt_type="database",
            status=_periodic_receipt_status(item),
            actual_write=bool(item.actual_write),
        )
        for item in executions
        if getattr(item, "receipt_id", None)
    )
    actual_write = any(bool(item.actual_write) for item in executions)
    statuses = {str(item.status) for item in executions}
    if "blocked" in statuses:
        business_status = "blocked"
    elif statuses and statuses <= {"duplicate"}:
        business_status = "duplicate"
    elif actual_write or operation == "query":
        business_status = "succeeded"
    else:
        business_status = "unchanged"
    blocking_reason = next(
        (
            str(item.execution.reason_code)
            for item in executions
            if str(item.status) == "blocked"
        ),
        "",
    )
    after = last.after
    before = executions[0].execution.before
    sections = dict(after.sections or {})
    changed_fields = tuple(
        dict.fromkeys(
            str(item.execution.command.patch.get("field") or "status")
            for item in executions
            if item.actual_write
        )
    )
    return (
        OperationOutcome(
            domain="report",
            operation=operation,
            object_ref=OutcomeObjectRef(
                object_type=f"{after.report_type}_report",
                stable_id=str(after.report_id),
                label=str(after.period_key),
                version=after.version,
            ),
            business_status=business_status,
            message_status="not_applicable",
            changed_fields=changed_fields,
            user_visible_snapshot={
                "report_type": str(after.report_type),
                "period_label": str(after.period_key),
                "accomplishments": list(sections.get("accomplishments", ())),
                "risks": list(sections.get("risks", ())),
                "next_plan": list(sections.get("next_plan", ())),
                "metrics": list(sections.get("metrics", ())),
            },
            blocking_reason=blocking_reason,
            receipt_refs=receipt_refs,
            state_transition=OutcomeStateTransition(str(before.status), str(after.status)),
            actual_write=actual_write,
            source_turn_id=source_turn_id,
        ),
    )


def daily_execution_outcomes(
    result: Any,
    *,
    source_turn_id: str,
) -> tuple[OperationOutcome, ...]:
    command_results = tuple(
        item for item in (getattr(result, "command_results", None) or ()) if isinstance(item, dict)
    )
    command_types = tuple(
        str((item.get("typed_command") or {}).get("command_type") or item.get("operation") or "")
        for item in command_results
    )
    operation = _daily_operation(command_types)
    receipt_refs = tuple(_dict_receipt_ref(item) for item in command_results if item.get("receipt_id"))
    actual_write = any(bool(item.get("actual_write", False)) for item in command_results)
    statuses = {str(item.get("status") or item.get("validation_status") or "") for item in command_results}
    if "blocked" in statuses:
        business_status = "blocked"
    elif statuses and statuses <= {"duplicate"}:
        business_status = "duplicate"
    elif actual_write:
        business_status = "succeeded"
    elif operation == "query":
        business_status = "succeeded"
    else:
        business_status = "unchanged"
    audits = [item.get("audit") for item in command_results if isinstance(item.get("audit"), dict)]
    after_version = max((int(item.get("after_version", 0)) for item in audits), default=None)
    changed_fields = tuple(
        dict.fromkeys(
            str((item.get("typed_command") or {}).get("patch", {}).get("field") or "")
            for item in command_results
            if str((item.get("typed_command") or {}).get("patch", {}).get("field") or "")
        )
    )
    blocking_reason = next(
        (
            str(item.get("reason") or item.get("reason_code") or "")
            for item in command_results
            if str(item.get("status") or item.get("validation_status") or "") == "blocked"
        ),
        "",
    )
    label = f"{getattr(result, 'report_date', '')}日报"
    return (
        OperationOutcome(
            domain="report",
            operation=operation,
            object_ref=OutcomeObjectRef(
                "daily_report",
                str(getattr(result, "report_id", "") or ""),
                label,
                after_version,
            ),
            business_status=business_status,
            message_status="not_applicable",
            changed_fields=changed_fields,
            user_visible_snapshot={
                "report_type": "daily",
                "period_label": str(getattr(result, "report_date", "") or ""),
                "today_work": list(getattr(result, "today_work", None) or []),
                "problems": list(getattr(result, "problems", None) or []),
                "tomorrow_plan": list(getattr(result, "tomorrow_plan", None) or []),
            },
            blocking_reason=blocking_reason,
            receipt_refs=receipt_refs,
            state_transition=OutcomeStateTransition(
                "unknown",
                str(getattr(result, "status", "") or "unknown"),
            ),
            actual_write=actual_write,
            source_turn_id=source_turn_id,
        ),
    )


def business_composition_outcomes(result: Any) -> tuple[OperationOutcome, ...]:
    outcomes: list[OperationOutcome] = []
    for action in tuple(getattr(result, "actions", ()) or ()):
        receipt = getattr(action, "receipt", None)
        block = getattr(action, "block", None)
        context = dict(getattr(action, "outcome_context", None) or {})
        compiled = str(getattr(action, "compiled_command_type", "") or "")
        semantic = str(getattr(action, "semantic_command_type", "") or "")
        if receipt is None:
            metadata = dict(getattr(block, "metadata", None) or {}) if block else {}
            selection = metadata.get("selection") if isinstance(metadata.get("selection"), dict) else {}
            candidates = selection.get("candidates") if isinstance(selection, dict) else []
            destination = str(context.get("destination") or "").strip()
            purpose = str(context.get("purpose") or "").strip()
            case_name = str(context.get("case_name") or "").strip()
            label = case_name or destination or "需要补充信息"
            snapshot = {"candidates": list(candidates or [])}
            if case_name:
                snapshot["case_name"] = case_name
            if destination:
                snapshot["destination"] = destination
            if purpose:
                snapshot["purpose"] = purpose
            outcomes.append(
                OperationOutcome(
                    domain=str(selection.get("domain") or _business_domain(compiled, semantic)),
                    operation=str(selection.get("operation") or _business_operation(compiled, semantic)),
                    object_ref=OutcomeObjectRef("selection", "", label),
                    business_status="blocked",
                    message_status="not_applicable",
                    changed_fields=(),
                    user_visible_snapshot=snapshot,
                    blocking_reason=str(getattr(block, "reason_code", "") or "execution_blocked"),
                    receipt_refs=(),
                    state_transition=OutcomeStateTransition("planned", "blocked"),
                    actual_write=False,
                    source_turn_id=str(getattr(result, "source_message_id", "") or ""),
                )
            )
            continue
        after = dict(getattr(receipt, "after", None) or {})
        before = dict(getattr(receipt, "before", None) or {})
        domain = _business_domain(compiled, semantic)
        operation = _business_operation(compiled, semantic)
        actual_write = bool(getattr(receipt, "actual_write", False))
        receipt_status = str(getattr(receipt, "status", "") or "failed")
        business_status = _business_status(domain, compiled, receipt_status, actual_write, after)
        snapshot = _business_snapshot(domain, compiled, context, before, after)
        label = str(
            snapshot.get("case_name")
            or snapshot.get("destination")
            or snapshot.get("title")
            or getattr(receipt, "resource_id", "")
            or domain
        )
        outcome_message_status = (
            str(after.get("message_status") or "scheduled")
            if domain == "followup" and compiled == "trigger_case_followup_now"
            and receipt_status in {"executed", "duplicate"}
            else "not_applicable"
        )
        outcomes.append(
            OperationOutcome(
                domain=domain,
                operation=operation,
                object_ref=OutcomeObjectRef(
                    str(getattr(receipt, "resource_type", "") or domain),
                    str(getattr(receipt, "resource_id", "") or ""),
                    label,
                    _optional_int(after.get("version")),
                ),
                business_status=business_status,
                message_status=outcome_message_status,
                changed_fields=_changed_fields(before, after),
                user_visible_snapshot=snapshot,
                blocking_reason=str(
                    getattr(receipt, "error_code", None)
                    or getattr(receipt, "failed_stage", None)
                    or ""
                ),
                receipt_refs=(
                    OutcomeReceiptRef(
                        str(getattr(receipt, "receipt_id", "") or ""),
                        "database",
                        receipt_status,
                        actual_write,
                    ),
                ),
                state_transition=OutcomeStateTransition(
                    str(before.get("status") or "unknown"),
                    str(after.get("status") or business_status),
                ),
                actual_write=actual_write,
                source_turn_id=str(getattr(result, "source_message_id", "") or ""),
                tenant_id=str(getattr(receipt, "tenant_id", "") or ""),
                user_id=str(getattr(receipt, "actor_user_id", "") or ""),
            )
        )
    return tuple(outcomes)


def notification_outcome(
    event: Any,
    *,
    travel_snapshot: dict[str, Any],
    source_turn_id: str,
    domain: str = "travel",
    conversation_id: str = "",
) -> OperationOutcome:
    """Map outbox transport state without upgrading acceptance to delivery."""
    raw_status = str(getattr(event, "status", "") or "failed")
    mapped = {
        "pending": "queued",
        "processing": "sending",
        "sent": "accepted_by_provider",
        "failed": "failed",
        "dead_letter": "failed",
        "cancelled": "cancelled",
    }.get(raw_status, "failed")
    external_message_id = str(getattr(event, "external_message_id", "") or "")
    receipt_status = "accepted_by_provider" if mapped == "accepted_by_provider" else mapped
    receipt = OutcomeReceiptRef(
        receipt_id=str(getattr(event, "notification_id", "") or "notification-outbox"),
        receipt_type="transport",
        status=receipt_status,
        actual_write=False,
        external_message_id=external_message_id,
        reliable_delivery_evidence=False,
    )
    return OperationOutcome(
        domain=domain,
        operation="notify",
        object_ref=OutcomeObjectRef(
            "case_followup_notification" if domain == "followup" else "travel_notification",
            str(getattr(event, "notification_id", "") or ""),
            str(
                travel_snapshot.get("case_name")
                or travel_snapshot.get("destination")
                or ("案件主动追问" if domain == "followup" else "出差协同通知")
            ),
        ),
        business_status=mapped,
        message_status=mapped,
        changed_fields=("message_status",),
        user_visible_snapshot=dict(travel_snapshot),
        blocking_reason=str(getattr(event, "error_message", "") or ""),
        receipt_refs=(receipt,),
        state_transition=OutcomeStateTransition("unknown", mapped),
        actual_write=False,
        source_turn_id=source_turn_id,
        tenant_id=str(getattr(event, "tenant_id", "") or ""),
        user_id=str(getattr(event, "recipient_user_id", "") or ""),
        conversation_id=conversation_id,
        idempotency_key=(
            f"notification-outcome:{getattr(event, 'tenant_id', '')}:"
            f"{getattr(event, 'notification_id', '')}:{mapped}"
        ),
    )


def text_outcome(
    text: str,
    *,
    source_turn_id: str,
    domain: str = "chat",
) -> OperationOutcome:
    if domain not in {"chat", "knowledge"}:
        raise ValueError("text outcome supports only existing read-only expression domains")
    field = "answer" if domain == "knowledge" else "text"
    return OperationOutcome(
        domain=domain,
        operation="query" if domain == "knowledge" else "reply",
        object_ref=OutcomeObjectRef(domain, source_turn_id, "本轮回复"),
        business_status="succeeded",
        message_status="not_applicable",
        changed_fields=(),
        user_visible_snapshot={field: str(text or "")},
        blocking_reason="",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("received", "replied"),
        actual_write=False,
        source_turn_id=source_turn_id,
    )


def _daily_operation(command_types: tuple[str, ...]) -> str:
    priority = (
        ("submit_report", "submit"),
        ("delete_item", "delete"),
        ("clear_report", "delete"),
        ("edit_item", "update"),
        ("merge_items", "update"),
        ("copy_report", "update"),
        ("append_item", "create"),
        ("query_report", "query"),
    )
    return next((operation for command, operation in priority if command in command_types), "query")


def _dict_receipt_ref(item: dict[str, Any]) -> OutcomeReceiptRef:
    status = str(item.get("status") or item.get("validation_status") or "unknown")
    if status == "authorized" and bool(item.get("actual_write", False)):
        status = "executed"
    return OutcomeReceiptRef(
        receipt_id=str(item.get("receipt_id") or ""),
        receipt_type="database",
        status=status,
        actual_write=bool(item.get("actual_write", False)),
    )


def _periodic_receipt_status(item: Any) -> str:
    status = str(getattr(item, "status", "") or "unknown")
    if status == "duplicate":
        return "duplicate"
    if status == "blocked":
        return "blocked"
    # "authorized" is the domain executor decision.  Once the SQL adapter
    # returns an actual_write receipt, the database operation has executed.
    if bool(getattr(item, "actual_write", False)):
        return "executed"
    return status


def _business_domain(compiled: str, semantic: str) -> str:
    value = compiled or semantic
    if value == "list_assigned_cases":
        return "knowledge"
    if value == "query_operation_status":
        return "knowledge"
    if "case_progress" in value:
        return "case_progress"
    if "followup" in value:
        return "followup"
    if "travel" in value:
        return "travel"
    if "party" in value or "knowledge" in value or "case_risk" in value:
        return "knowledge"
    return "chat"


def _business_operation(compiled: str, semantic: str) -> str:
    value = compiled or semantic
    if value.startswith("create_case_progress"):
        return "create"
    if value.startswith("update_case_progress"):
        return "update"
    if value.startswith("delete_case_progress"):
        return "delete"
    if value.startswith("query"):
        return "query"
    if value.startswith("create_travel") or value.startswith("record_travel"):
        return "register"
    if value.startswith("update_travel"):
        return "update"
    if value.startswith("respond_travel"):
        return "respond"
    if value.startswith("update_case_followup_policy"):
        return "update"
    if value.startswith("trigger_case_followup_now"):
        return "create_task"
    if value.startswith("snooze_case_followup"):
        return "snooze"
    return "query"


def _business_status(
    domain: str,
    compiled: str,
    receipt_status: str,
    actual_write: bool,
    after: dict[str, Any],
) -> str:
    if receipt_status == "duplicate":
        return "duplicate"
    if receipt_status == "blocked":
        return "blocked"
    if receipt_status == "failed":
        return "failed"
    if domain == "travel" and compiled == "create_travel_intent" and actual_write:
        return "registered"
    if domain == "travel" and compiled == "update_travel_intent" and actual_write:
        return (
            "cancelled"
            if str(after.get("status") or "") == "cancelled"
            else "changed"
        )
    if domain == "travel" and compiled == "respond_travel_collaboration":
        return {
            "accepted_by_one": "accepted_by_one_party",
            "accepted": "accepted_by_both",
            "declined": "declined",
            "cancelled": "cancelled",
            "expired": "expired",
        }.get(str(after.get("status") or ""), "succeeded")
    return "succeeded" if receipt_status == "executed" else "failed"


def _business_snapshot(
    domain: str,
    compiled: str,
    context: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    if domain == "case_progress":
        source = after or before
        items = source.get("items") if isinstance(source.get("items"), list) else []
        content = str(source.get("summary") or context.get("content") or "")
        if items:
            content = "\n".join(
                f"{item.get('occurred_at') or ''} {item.get('summary') or ''}".strip()
                for item in items[:10]
                if isinstance(item, dict)
            )
        extraction = context.get("case_fact_extraction")
        extraction = extraction if isinstance(extraction, dict) else {}
        next_actions = source.get("next_actions")
        if not isinstance(next_actions, list):
            next_actions = extraction.get("next_actions")
        if not isinstance(next_actions, list):
            next_actions = []
        return {
            "case_name": str(context.get("case_name") or source.get("case_name") or "该案件"),
            "content": content,
            "next_actions": [
                str(value)
                for value in next_actions
                if str(value or "").strip()
            ],
        }
    if domain == "travel":
        return {
            "destination": str(context.get("destination") or after.get("destination_normalized") or ""),
            "date_label": str(context.get("date_label") or ""),
            "purpose": str(context.get("purpose") or after.get("purpose_summary") or ""),
            "counterparty": str(context.get("counterparty") or ""),
        }
    if domain == "knowledge":
        if compiled == "list_assigned_cases":
            return {"answer": _assigned_case_inventory_answer(after)}
        if compiled == "query_operation_status":
            return {"answer": str(after.get("answer") or "没有查到可核验的操作结果。")}
        return {"answer": str(context.get("answer") or after.get("answer") or _knowledge_answer(after))}
    if domain == "followup":
        if compiled == "snooze_case_followup":
            return {
                "case_name": str(context.get("case_name") or after.get("case_name") or "该案件"),
                "snoozed_until": str(
                    after.get("snoozed_until") or context.get("snoozed_until") or ""
                ),
            }
        if compiled == "trigger_case_followup_now":
            return {
                "case_name": str(context.get("case_name") or after.get("case_name") or "该案件"),
                "due_at_label": str(after.get("due_at") or ""),
                "question_summary": str(after.get("question_summary") or "案件进展"),
            }
        return {
            "case_name": str(context.get("case_name") or after.get("case_name") or "该案件"),
            "cadence_type": str(after.get("cadence_type") or context.get("cadence_type") or ""),
            "next_due_at": str(after.get("next_due_at") or ""),
            "enabled": bool(after.get("enabled", False)),
            "hearing_reminders_enabled": bool(after.get("hearing_reminders_enabled", False)),
            "stage_transition_enabled": bool(after.get("stage_transition_enabled", False)),
            "node_transition_enabled": bool(after.get("node_transition_enabled", False)),
        }
    return {"text": str(context.get("text") or "")}


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _knowledge_answer(after: dict[str, Any]) -> str:
    party = after.get("party") if isinstance(after.get("party"), dict) else {}
    cases = after.get("cases") if isinstance(after.get("cases"), list) else []
    if party or cases:
        name = str(party.get("canonical_name") or "该主体")
        lines = [f"{name}关联案件 {after.get('case_count', len(cases))} 件："]
        for item in cases[:10]:
            if not isinstance(item, dict):
                continue
            identifier = item.get("case_number") or item.get("external_case_id") or ""
            role_status = "/".join(
                value
                for value in (
                    str(item.get("role_type") or ""),
                    str(item.get("status") or ""),
                )
                if value
            )
            suffix = f"（{role_status}）" if role_status else ""
            lines.append(f"- {identifier} {item.get('case_name') or ''}{suffix}".strip())
        return "\n".join(lines)
    return ""


def _assigned_case_inventory_answer(after: dict[str, Any]) -> str:
    cases = after.get("cases") if isinstance(after.get("cases"), list) else []
    lines = [f"你当前负责 {after.get('case_count', len(cases))} 件案件："]
    for index, item in enumerate(cases, start=1):
        if not isinstance(item, dict):
            continue
        case_number = str(item.get("case_number") or "").strip()
        case_name = str(item.get("case_name") or "未命名案件").strip()
        case_type = case_type_label(item.get("case_type"))
        stage = case_stage_label(item.get("stage") or item.get("status"))
        metadata = " / ".join(value for value in (case_type, stage) if value)
        suffix = f"（{metadata}）" if metadata else ""
        number_prefix = f"{case_number} " if case_number else ""
        lines.append(f"{index}. {number_prefix}{case_name}{suffix}")
    if not cases:
        lines.append("目前没有分配给你的案件。")
    return "\n".join(lines)
