from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import re
import uuid
from zoneinfo import ZoneInfo
from typing import Any, TYPE_CHECKING

from app.agent2.daily_commands import DailyCommand
from app.agent2.daily_command_compiler import TypedDailyBatchResult, apply_legacy_daily_commands_as_typed
from app.agent2.daily_edit_intent import looks_like_parenthetical_delete, looks_like_spoken_correction
from app.agent2.daily_state import (
    DRAFT_ITEM_IDS_KEY,
    REPORT_FIELD_ORDER,
    REPORT_FIELDS,
    clear_pending_daily_candidate,
    focus_item,
    pending_daily_candidate,
)
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot


TYPED_REPORT_VERSION_KEY = "_agent2_report_version"
TYPED_COMMAND_KEYS_KEY = "_agent2_typed_command_keys"
TYPED_AUDIT_KEY = "_agent2_typed_audit"

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import DailyReport, User


@dataclass(frozen=True)
class Agent2DailyExecutionResult:
    report_id: str | None
    report_date: date
    status: str
    message: str
    report_saved: bool
    read_only: bool
    today_work: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    tomorrow_plan: list[str] = field(default_factory=list)
    command_results: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DailyCommandApplication:
    today_work: list[str]
    problems: list[str]
    tomorrow_plan: list[str]
    status: str
    changed: bool
    read_only: bool
    actions: list[dict[str, Any]]
    item_ids: dict[str, list[str]] = field(default_factory=dict)


def agent2_daily_enabled_for_user(settings: Any, user: User) -> bool:
    if not bool(getattr(settings, "agent2_daily_enabled", False)):
        return False
    configured = _csv_set(str(getattr(settings, "agent2_daily_enabled_user_ids", "") or ""))
    if "*" in configured:
        return True
    candidates = {
        str(getattr(user, "id", "") or ""),
        str(getattr(user, "dingtalk_user_id", "") or ""),
        str(getattr(user, "employee_no", "") or ""),
    }
    return any(candidate and candidate in configured for candidate in candidates)


def agent2_daily_report_version(report: "DailyReport | None") -> int:
    return _typed_report_version(getattr(report, "section_status", None))


async def execute_agent2_daily_commands(
    session: "AsyncSession",
    *,
    user: "User",
    raw_input: str,
    source: str,
    commands: list[DailyCommand],
    settings: Any,
    report_date: date | None = None,
    message_id: str = "",
    expected_report_version: int | None = None,
) -> Agent2DailyExecutionResult:
    from app.repositories import acquire_daily_report_advisory_lock, build_report_interaction_snapshot, get_report, upsert_daily_report

    timezone_name = getattr(user, "timezone", None) or getattr(settings, "timezone", "Asia/Shanghai")
    received_at = _now_in_timezone(timezone_name)
    base_report_date = report_date or received_at.date()
    if _should_block_historical_daily_mutation_after_cutoff(raw_input, received_at):
        target_report_date = _historical_report_date_from_raw_input(base_report_date, raw_input)
        existing = await get_report(session, user.id, target_report_date)
        actions = [
            {
                "operation": "no_write",
                "changed": False,
                "reason": "historical daily reports are read-only after the 09:00 cutoff; use query or copy instead",
                "safety_flags": ["historical_daily_mutation_blocked_after_cutoff"],
            }
        ]
        return Agent2DailyExecutionResult(
            report_id=str(getattr(existing, "id", "") or "") or None,
            report_date=target_report_date,
            status=str(getattr(existing, "status", "") or "collecting"),
            message=_no_change_message(actions),
            report_saved=False,
            read_only=True,
            today_work=list(getattr(existing, "today_work", []) or []),
            problems=list(getattr(existing, "problems", []) or []),
            tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
            command_results=actions,
        )
    target_report_date = _report_date_for_commands(base_report_date, commands)

    await acquire_daily_report_advisory_lock(session, user.id, target_report_date)
    existing = await get_report(session, user.id, target_report_date)
    before_snapshot = build_report_interaction_snapshot(existing)
    previous_report = await _previous_report_for_copy(session, user=user, report_date=target_report_date, commands=commands)

    typed_batch = _apply_typed_command_adapter(
        commands=commands,
        message_id=message_id,
        user=user,
        report_date=target_report_date,
        existing=existing,
        previous_report=previous_report,
        expected_report_version=expected_report_version,
    )
    if typed_batch is not None and typed_batch.status != "unsupported":
        application = _daily_application_from_typed_batch(typed_batch, commands)
    else:
        application = apply_commands_to_snapshot(
            today_work=list(getattr(existing, "today_work", []) or []),
            problems=list(getattr(existing, "problems", []) or []),
            tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
            status=str(getattr(existing, "status", "") or "collecting"),
            commands=commands,
            previous_report=previous_report,
            section_status=getattr(existing, "section_status", None),
        )

    if application.read_only:
        return Agent2DailyExecutionResult(
            report_id=str(getattr(existing, "id", "") or "") or None,
            report_date=target_report_date,
            status=str(getattr(existing, "status", "") or "collecting"),
            message=_read_only_message(existing, target_report_date, application.actions),
            report_saved=False,
            read_only=True,
            today_work=list(getattr(existing, "today_work", []) or []),
            problems=list(getattr(existing, "problems", []) or []),
            tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
            command_results=application.actions,
        )

    if not application.changed:
        return Agent2DailyExecutionResult(
            report_id=str(getattr(existing, "id", "") or "") or None,
            report_date=target_report_date,
            status=str(getattr(existing, "status", "") or "collecting"),
            message=_no_change_message(application.actions),
            report_saved=False,
            read_only=False,
            today_work=application.today_work,
            problems=application.problems,
            tomorrow_plan=application.tomorrow_plan,
            command_results=application.actions,
        )

    section_status = {
        **(getattr(existing, "section_status", None) or {}),
        "agent2_last_commands": [command.as_dict() for command in commands],
    }
    if typed_batch is not None and typed_batch.status == "executed":
        section_status[TYPED_REPORT_VERSION_KEY] = typed_batch.after.version
        existing_keys = _typed_command_keys(getattr(existing, "section_status", None))
        new_keys = [execution.command.idempotency_key for execution in typed_batch.executions]
        section_status[TYPED_COMMAND_KEYS_KEY] = [*existing_keys, *new_keys][-100:]
        existing_audits = _typed_audit_records(getattr(existing, "section_status", None))
        new_audits = [execution.audit.as_dict() for execution in typed_batch.executions]
        section_status[TYPED_AUDIT_KEY] = [*existing_audits, *new_audits][-100:]
    _attach_agent2_item_ids(
        section_status,
        existing=existing,
        today_work=application.today_work,
        problems=application.problems,
        tomorrow_plan=application.tomorrow_plan,
        item_ids=application.item_ids,
    )
    _attach_agent2_edit_memory(section_status, application)

    report = await upsert_daily_report(
        session,
        user=user,
        report_date=target_report_date,
        raw_input=raw_input,
        source=source,
        today_work=application.today_work,
        problems=application.problems,
        tomorrow_plan=application.tomorrow_plan,
        emotion="",
        completeness_score=_completeness(application.today_work, application.problems, application.tomorrow_plan),
        status=application.status,
        section_status=section_status,
        llm_model="agent2",
        llm_payload={
            "agent2": True,
            "commands": [command.as_dict() for command in commands],
            "command_results": application.actions,
            "typed_commands": [execution.command.as_dict() for execution in typed_batch.executions] if typed_batch is not None else [],
            "typed_audit": [execution.audit.as_dict() for execution in typed_batch.executions] if typed_batch is not None else [],
        },
        received_at=received_at,
        confirmation_type="user_confirmed" if _has_confirm_submit(commands, application.status) else "none",
        confirmed_by_user=_has_confirm_submit(commands, application.status),
        quality_warning=None,
        last_modified_by_user=True,
        last_modified_at=received_at,
        pending_confirmation_at=None,
        auto_submit_at=None,
        replace_sections=True,
        report_id_override=typed_batch.after.report_id if typed_batch is not None and existing is None else None,
    )
    after_snapshot = build_report_interaction_snapshot(report)
    if not bool(getattr(settings, "shadow_memory_enabled", False)):
        await _create_agent2_ledger_event(
            session,
            user=user,
            report=report,
            report_date=target_report_date,
            message_text=raw_input,
            commands=commands,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            actions=application.actions,
        )
    return Agent2DailyExecutionResult(
        report_id=str(report.id),
        report_date=target_report_date,
        status=application.status,
        message=_write_message(application, target_report_date),
        report_saved=True,
        read_only=False,
        today_work=list(report.today_work or []),
        problems=list(report.problems or []),
        tomorrow_plan=list(report.tomorrow_plan or []),
        command_results=application.actions,
    )


def _apply_typed_command_adapter(
    *,
    commands: list[DailyCommand],
    message_id: str,
    user: "User",
    report_date: date,
    existing: "DailyReport | None",
    previous_report: "DailyReport | None",
    expected_report_version: int | None,
) -> TypedDailyBatchResult | None:
    if not str(message_id or "").strip():
        return None
    snapshot = _typed_snapshot(user=user, report_date=report_date, report=existing)
    return apply_legacy_daily_commands_as_typed(
        commands,
        message_id=message_id,
        snapshot=snapshot,
        actor_user_id=user.id,
        expected_report_version=(
            expected_report_version
            if expected_report_version is not None
            else snapshot.version
        ),
        executed_idempotency_keys=_typed_command_keys(getattr(existing, "section_status", None)),
        previous_snapshot=(
            _typed_snapshot(
                user=user,
                report_date=getattr(previous_report, "report_date", report_date),
                report=previous_report,
            )
            if previous_report is not None
            else None
        ),
    )


def _typed_snapshot(
    *,
    user: "User",
    report_date: date,
    report: "DailyReport | None",
) -> DailyReportMutationSnapshot:
    today_work = list(getattr(report, "today_work", []) or [])
    problems = list(getattr(report, "problems", []) or [])
    tomorrow_plan = list(getattr(report, "tomorrow_plan", []) or [])
    item_ids = _item_ids_from_section_status(
        getattr(report, "section_status", None),
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
    )
    report_id = getattr(report, "id", None) or uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"agent2-daily-report:{user.id}:{report_date.isoformat()}",
    )
    return DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=user.id,
        version=_typed_report_version(getattr(report, "section_status", None)),
        status=str(getattr(report, "status", "") or "collecting"),
        today_work=tuple(today_work),
        problems=tuple(problems),
        tomorrow_plan=tuple(tomorrow_plan),
        item_ids={field: tuple(item_ids.get(field, [])) for field in REPORT_FIELD_ORDER},
    )


def _typed_report_version(section_status: dict[str, Any] | None) -> int:
    try:
        return max(0, int((section_status or {}).get(TYPED_REPORT_VERSION_KEY, 0)))
    except (TypeError, ValueError):
        return 0


def _typed_command_keys(section_status: dict[str, Any] | None) -> list[str]:
    values = (section_status or {}).get(TYPED_COMMAND_KEYS_KEY, [])
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value).strip()]


def _typed_audit_records(section_status: dict[str, Any] | None) -> list[dict[str, Any]]:
    values = (section_status or {}).get(TYPED_AUDIT_KEY, [])
    if not isinstance(values, list):
        return []
    return [dict(value) for value in values if isinstance(value, dict)]


def _daily_application_from_typed_batch(
    batch: TypedDailyBatchResult,
    legacy_commands: list[DailyCommand],
) -> DailyCommandApplication:
    if batch.status != "executed":
        action: dict[str, Any] = {
            "operation": legacy_commands[0].operation if legacy_commands else "unknown",
            "changed": False,
            "reason": batch.reason_code,
            "validation_status": "blocked",
        }
        if batch.compilations:
            action["expected_reply_type"] = batch.compilations[-1].expected_reply_type
        if batch.executions:
            action["typed_command"] = batch.executions[-1].command.as_dict()
            action["audit"] = batch.executions[-1].audit.as_dict()
        return DailyCommandApplication(
            today_work=list(batch.before.today_work),
            problems=list(batch.before.problems),
            tomorrow_plan=list(batch.before.tomorrow_plan),
            status=batch.before.status,
            changed=False,
            read_only=False,
            actions=[action],
            item_ids={field: list(batch.before.item_ids.get(field, ())) for field in REPORT_FIELD_ORDER},
        )

    actions = [
        _typed_execution_action(
            execution,
            legacy_commands[batch.execution_command_indices[index]]
            if index < len(batch.execution_command_indices) and batch.execution_command_indices[index] < len(legacy_commands)
            else None,
        )
        for index, execution in enumerate(batch.executions)
    ]
    return DailyCommandApplication(
        today_work=list(batch.after.today_work),
        problems=list(batch.after.problems),
        tomorrow_plan=list(batch.after.tomorrow_plan),
        status=batch.after.status,
        changed=batch.after != batch.before,
        read_only=bool(batch.executions) and all(
            execution.command.command_type == "query_report"
            for execution in batch.executions
        ),
        actions=actions,
        item_ids={field: list(batch.after.item_ids.get(field, ())) for field in REPORT_FIELD_ORDER},
    )


def _typed_execution_action(execution: Any, legacy_command: DailyCommand | None) -> dict[str, Any]:
    command = execution.command
    action: dict[str, Any] = {
        "operation": getattr(legacy_command, "operation", "") or command.command_type,
        "typed_command_type": command.command_type,
        "typed_command": command.as_dict(),
        "validation_status": execution.validation.status,
        "reason": execution.validation.reason_code,
        "audit": execution.audit.as_dict(),
        "changed": execution.changed,
        "read_only": command.command_type == "query_report",
    }
    if command.command_type == "append_item":
        field_name = str(command.patch.get("field") or "")
        before_ids = set(execution.before.item_ids.get(field_name, ()))
        action.update(
            {
                "target_field": field_name,
                "edit_action": "append_item",
                "item_ids": [item_id for item_id in execution.after.item_ids.get(field_name, ()) if item_id not in before_ids],
            }
        )
    elif command.command_type in {"edit_item", "delete_item", "merge_items"}:
        field_name = _field_for_typed_target(execution.before, command.target_item_ids[0])
        edit_action = {
            "edit_item": "replace_item",
            "delete_item": "delete_item",
            "merge_items": "merge_items",
        }[command.command_type]
        action.update(
            {
                "target_field": field_name,
                "edit_action": edit_action,
                "item_ids": list(command.target_item_ids),
            }
        )
        if command.command_type == "delete_item":
            action["removed_item_ids"] = list(command.target_item_ids)
        if command.command_type == "merge_items":
            action["merged_item_ids"] = list(command.target_item_ids)
    return action


def _field_for_typed_target(snapshot: DailyReportMutationSnapshot, target_item_id: str) -> str:
    for field_name in REPORT_FIELD_ORDER:
        if target_item_id in snapshot.item_ids.get(field_name, ()):
            return field_name
    return ""


def apply_commands_to_snapshot(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    status: str,
    commands: list[DailyCommand],
    previous_report: DailyReport | None = None,
    section_status: dict[str, Any] | None = None,
) -> DailyCommandApplication:
    original = (list(today_work), list(problems), list(tomorrow_plan), status)
    current_status = status or "collecting"
    actions: list[dict[str, Any]] = []
    read_only = False
    item_ids = _item_ids_from_section_status(
        section_status,
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
    )

    for command in commands:
        operation = command.operation
        if operation in {"query_current", "query_history", "begin_edit"}:
            read_only = True
            actions.append({"operation": operation, "changed": False, "read_only": True})
            continue
        if current_status == "completed" and operation in {"fill", "edit", "clear", "copy_previous", "copy_current_to_tomorrow"}:
            actions.append({"operation": operation, "changed": False, "reason": "completed_report_locked"})
            continue
        if operation == "fill":
            target = command.target_field
            values = _clean_items(command.content)
            if target in REPORT_FIELDS and values:
                if target == "today_work":
                    today_work, item_ids[target], added_ids = _merge_ordered_with_item_ids(today_work, item_ids[target], values, target)
                elif target == "problems":
                    problems, item_ids[target], added_ids = _merge_ordered_with_item_ids(problems, item_ids[target], values, target)
                elif target == "tomorrow_plan":
                    tomorrow_plan, item_ids[target], added_ids = _merge_ordered_with_item_ids(tomorrow_plan, item_ids[target], values, target)
                else:
                    added_ids = []
            else:
                added_ids = []
            actions.append(
                {
                    "operation": operation,
                    "target_field": target,
                    "item_count": len(values),
                    "item_ids": added_ids,
                    "changed": bool(added_ids),
                }
            )
        elif operation == "edit":
            edit_result = _apply_edit_command(
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
                command=command,
                item_ids=item_ids,
                section_status=section_status,
            )
            today_work = edit_result["today_work"]
            problems = edit_result["problems"]
            tomorrow_plan = edit_result["tomorrow_plan"]
            item_ids = edit_result["item_ids"]
            actions.append(edit_result["action"])
        elif operation == "clear":
            target = command.target_field
            if target == "all":
                removed_item_ids = _all_item_ids(item_ids)
                today_work, problems, tomorrow_plan = [], [], []
                item_ids = _empty_item_ids()
                current_status = "collecting"
                actions.append({"operation": operation, "target_field": target, "removed_item_ids": removed_item_ids, "changed": bool(removed_item_ids)})
            elif target in REPORT_FIELDS:
                before_len = len(_values_for_field(today_work, problems, tomorrow_plan, target))
                removed_item_ids = list(item_ids.get(target, []))
                today_work, problems, tomorrow_plan = _set_field_values(today_work, problems, tomorrow_plan, target, [])
                item_ids[target] = []
                current_status = "collecting"
                actions.append({"operation": operation, "target_field": target, "removed_item_ids": removed_item_ids, "changed": before_len > 0})
            else:
                actions.append({"operation": operation, "target_field": target, "changed": False, "reason": "missing_target_field"})
        elif operation == "copy_previous":
            if previous_report is None:
                actions.append({"operation": operation, "changed": False, "reason": "previous_report_missing"})
                continue
            target = command.target_field
            if target == "all":
                today_work = list(previous_report.today_work or [])
                problems = list(previous_report.problems or [])
                tomorrow_plan = list(previous_report.tomorrow_plan or [])
                item_ids = _make_item_ids_for_values(today_work=today_work, problems=problems, tomorrow_plan=tomorrow_plan)
                copied_item_ids = _all_item_ids(item_ids)
            elif target in REPORT_FIELDS:
                incoming_values = _values_for_field(
                    list(previous_report.today_work or []),
                    list(previous_report.problems or []),
                    list(previous_report.tomorrow_plan or []),
                    target,
                )
                before_values = _values_for_field(today_work, problems, tomorrow_plan, target)
                if _field_already_contains_all(before_values, incoming_values):
                    merged_values = list(before_values)
                    merged_ids = list(item_ids[target])
                    copied_item_ids = []
                else:
                    merged_values = list(incoming_values)
                    merged_ids = _make_item_ids_for_values(
                        today_work=merged_values if target == "today_work" else [],
                        problems=merged_values if target == "problems" else [],
                        tomorrow_plan=merged_values if target == "tomorrow_plan" else [],
                    )[target]
                    copied_item_ids = list(merged_ids)
                today_work, problems, tomorrow_plan = _set_field_values(
                    today_work,
                    problems,
                    tomorrow_plan,
                    target,
                    merged_values,
                )
                item_ids[target] = merged_ids
            else:
                actions.append({"operation": operation, "target_field": target, "changed": False, "reason": "missing_target_field"})
                continue
            current_status = "collecting"
            actions.append(
                {
                    "operation": operation,
                    "target_field": target,
                    "changed": bool(copied_item_ids) if target in REPORT_FIELDS else True,
                    "source_report_id": str(previous_report.id),
                    "item_ids": copied_item_ids,
                }
            )
        elif operation == "copy_current_to_tomorrow":
            if not today_work:
                actions.append({"operation": operation, "target_field": "tomorrow_plan", "changed": False, "reason": "today_work_empty"})
                continue
            before = list(tomorrow_plan)
            tomorrow_plan, item_ids["tomorrow_plan"], added_ids = _merge_ordered_with_item_ids(
                tomorrow_plan,
                item_ids["tomorrow_plan"],
                list(today_work),
                "tomorrow_plan",
            )
            current_status = "collecting"
            actions.append(
                {
                    "operation": operation,
                    "target_field": "tomorrow_plan",
                    "item_count": len(today_work),
                    "item_ids": added_ids,
                    "changed": before != tomorrow_plan,
                }
            )
        elif operation == "complete_previous_plan":
            if previous_report is None:
                actions.append({"operation": operation, "changed": False, "reason": "previous_plan_missing"})
                continue
            completed_items = _completed_previous_plan_items(list(previous_report.tomorrow_plan or []))
            if not completed_items:
                actions.append({"operation": operation, "changed": False, "reason": "previous_plan_empty"})
                continue
            before = list(today_work)
            today_work, item_ids["today_work"], added_ids = _merge_ordered_with_item_ids(
                today_work,
                item_ids["today_work"],
                completed_items,
                "today_work",
            )
            current_status = "collecting"
            actions.append(
                {
                    "operation": operation,
                    "target_field": "today_work",
                    "item_count": len(completed_items),
                    "item_ids": added_ids,
                    "source_report_id": str(previous_report.id),
                    "changed": before != today_work,
                }
            )
        elif operation == "revoke":
            if current_status != "completed":
                actions.append({"operation": operation, "changed": False, "reason": "report_not_completed"})
                continue
            current_status = "collecting"
            actions.append({"operation": operation, "changed": True})
        elif operation == "confirm":
            if today_work and problems and tomorrow_plan:
                current_status = "completed"
                actions.append({"operation": operation, "changed": True})
            else:
                actions.append({"operation": operation, "changed": False, "reason": "incomplete_report"})
        elif operation == "no_write":
            actions.append(
                {
                    "operation": operation,
                    "changed": False,
                    "reason": command.reason or "no_write",
                    "safety_flags": list(command.safety_flags),
                }
            )
        else:
            actions.append({"operation": operation, "changed": False, "reason": "unsupported_by_agent2_direct_executor"})

    changed = original != (today_work, problems, tomorrow_plan, current_status)
    return DailyCommandApplication(
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        status=current_status,
        changed=changed,
        read_only=read_only and not changed,
        actions=actions,
        item_ids=item_ids,
    )


def agent2_daily_should_fallback_to_legacy(actions: list[dict[str, Any]]) -> bool:
    # Once Agent2 has claimed a daily-report edit, unresolved targets should be
    # handled as a clear no-change result instead of handing the turn back to
    # the legacy daily writer, which may guess and write the wrong thing.
    return False


def _apply_edit_command(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    command: DailyCommand,
    item_ids: dict[str, list[str]] | None = None,
    section_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = _command_text(command)
    preferred_field = _field_from_text(text) or (command.target_field if command.target_field in REPORT_FIELDS else "")
    field_values = {
        "today_work": list(today_work),
        "problems": list(problems),
        "tomorrow_plan": list(tomorrow_plan),
    }
    field_item_ids = _normalize_item_ids_for_values(item_ids, field_values)
    recent_target = _recent_item_target_from_text(text, preferred_field, field_values, section_status)
    if recent_target is not None and not _edit_item_indices_from_text(text):
        preferred_field, recent_index = recent_target
        indices = [recent_index]
    else:
        resolved_field, indices = _resolve_item_indices_for_edit(text, preferred_field, field_values, section_status)
        if resolved_field:
            preferred_field = resolved_field

    field_replacement = _field_replacement_from_text(text)
    if field_replacement is not None:
        field, replacement = field_replacement
        field_values[field] = [replacement]
        previous_ids = list(field_item_ids[field])
        field_item_ids[field] = [_make_item_id(field, 1, replacement)]
        return _edit_result(
            field_values,
            field_item_ids,
            {
                "operation": "edit",
                "edit_action": "replace_field",
                "target_field": field,
                "item_ids": list(field_item_ids[field]),
                "removed_item_ids": previous_ids,
                "changed": True,
            },
        )

    parenthetical_delete = _parenthetical_delete_edit_result(
        text,
        preferred_field,
        field_values,
        field_item_ids,
        indices,
        section_status,
    )
    if parenthetical_delete is not None:
        return parenthetical_delete

    spoken_correction = _spoken_correction_edit_result(
        text,
        preferred_field,
        field_values,
        field_item_ids,
        indices,
        section_status,
    )
    if spoken_correction is not None:
        return spoken_correction

    negative_replacement = _negative_replacement_edit_result(
        text,
        preferred_field,
        field_values,
        field_item_ids,
        indices,
        section_status,
    )
    if negative_replacement is not None:
        return negative_replacement

    delete_append_edit = _delete_append_edit_from_text(text)
    if delete_append_edit is not None:
        delete_value, append_value = delete_append_edit
        source_field = _field_for_unique_or_preferred_text(field_values, preferred_field, delete_value)
        if source_field:
            before = list(field_values[source_field])
            delete_indices = [index for index, value in enumerate(before, start=1) if delete_value in value]
            if delete_indices and append_value:
                removed_ids = _select_by_indices(field_item_ids[source_field], delete_indices)
                field_values[source_field] = _delete_by_indices(before, delete_indices)
                field_item_ids[source_field] = _delete_by_indices(field_item_ids[source_field], delete_indices)
                field_values[source_field], field_item_ids[source_field], added_ids = _merge_ordered_with_item_ids(
                    field_values[source_field],
                    field_item_ids[source_field],
                    [append_value],
                    source_field,
                )
                return _edit_result(
                    field_values,
                    field_item_ids,
                    {
                        "operation": "edit",
                        "edit_action": "delete_and_append_item",
                        "target_field": source_field,
                        "item_indices": delete_indices,
                        "removed_item_ids": removed_ids,
                        "item_ids": added_ids,
                        "deleted_text": delete_value,
                        "appended_text": append_value,
                        "changed": before != field_values[source_field],
                    },
                )
        return _unsupported_edit_result(today_work, problems, tomorrow_plan, "delete_append_unresolved", item_ids=field_item_ids)

    if _has_delete_item_intent(text) and _has_merge_intent(text):
        return _unsupported_edit_result(today_work, problems, tomorrow_plan, "compound_edit_unresolved", item_ids=field_item_ids)

    if _has_move_intent(text) and indices:
        source_field = _source_field_for_indices(preferred_field, field_values, indices)
        target_field = _move_destination_field(text, source_field)
        if source_field and target_field and source_field != target_field:
            selected = _select_by_indices(field_values[source_field], indices)
            selected_ids = _select_by_indices(field_item_ids[source_field], indices)
            if selected:
                field_values[source_field] = _delete_by_indices(field_values[source_field], indices)
                field_item_ids[source_field] = _delete_by_indices(field_item_ids[source_field], indices)
                field_values[target_field], field_item_ids[target_field], moved_ids = _merge_ordered_with_item_ids(
                    field_values[target_field],
                    field_item_ids[target_field],
                    selected,
                    target_field,
                    incoming_ids=selected_ids,
                )
                target_indices = _indices_for_item_ids(field_item_ids[target_field], moved_ids)
                return _edit_result(
                    field_values,
                    field_item_ids,
                    {
                        "operation": "edit",
                        "edit_action": "move_item",
                        "source_field": source_field,
                        "target_field": target_field,
                        "item_indices": indices,
                        "item_ids": selected_ids,
                        "target_item_indices": target_indices,
                        "changed": True,
                    },
                )
        return _unsupported_edit_result(today_work, problems, tomorrow_plan, "move_target_unresolved", item_ids=field_item_ids)

    if _has_merge_intent(text) and indices:
        source_field = _source_field_for_indices(preferred_field, field_values, indices)
        if source_field:
            merged = _merge_items_by_indices(field_values[source_field], indices)
            if merged is not None:
                merged_ids = _select_by_indices(field_item_ids[source_field], indices)
                first_id = merged_ids[0] if merged_ids else _make_item_id(source_field, min(indices), merged[min(indices) - 1])
                field_values[source_field] = merged
                field_item_ids[source_field] = _merge_item_ids_by_indices(field_item_ids[source_field], indices, first_id)
                return _edit_result(
                    field_values,
                    field_item_ids,
                    {
                        "operation": "edit",
                        "edit_action": "merge_items",
                        "target_field": source_field,
                        "item_indices": indices,
                        "item_ids": [first_id],
                        "merged_item_ids": merged_ids,
                        "changed": True,
                    },
                )
        return _unsupported_edit_result(today_work, problems, tomorrow_plan, "merge_indices_unresolved", item_ids=field_item_ids)

    if _has_delete_item_intent(text) and not indices and _has_deictic_item_reference(text):
        return _unsupported_edit_result(today_work, problems, tomorrow_plan, "ambiguous_target", item_ids=field_item_ids)

    if _has_delete_item_intent(text) and not indices and _looks_like_field_clear(text, preferred_field):
        removed_ids = list(field_item_ids[preferred_field])
        before = list(field_values[preferred_field])
        field_values[preferred_field] = []
        field_item_ids[preferred_field] = []
        return _edit_result(
            field_values,
            field_item_ids,
            {
                "operation": "edit",
                "edit_action": "clear_field",
                "target_field": preferred_field,
                "removed_item_ids": removed_ids,
                "changed": bool(before),
            },
        )

    if _has_delete_item_intent(text) and indices:
        source_field = _source_field_for_indices(preferred_field, field_values, indices)
        if source_field:
            before = list(field_values[source_field])
            removed_ids = _select_by_indices(field_item_ids[source_field], indices)
            field_values[source_field] = _delete_by_indices(before, indices)
            field_item_ids[source_field] = _delete_by_indices(field_item_ids[source_field], indices)
            return _edit_result(
                field_values,
                field_item_ids,
                {
                    "operation": "edit",
                    "edit_action": "delete_item",
                    "target_field": source_field,
                    "item_indices": indices,
                    "item_ids": removed_ids,
                    "removed_item_ids": removed_ids,
                    "changed": before != field_values[source_field],
                },
            )
        return _unsupported_edit_result(today_work, problems, tomorrow_plan, "delete_indices_unresolved", item_ids=field_item_ids)

    if not _edit_item_indices_from_text(text):
        focused_candidate_edit = _focused_candidate_edit_result_from_text(
            text,
            field_values,
            field_item_ids,
            section_status,
        )
        if focused_candidate_edit is not None:
            return focused_candidate_edit

    indexed_replacement = _indexed_replacement_from_text(text)
    if indexed_replacement and indices:
        source_field = _source_field_for_indices(preferred_field, field_values, indices)
        if source_field:
            selected_ids = _select_by_indices(field_item_ids[source_field], indices)
            replaced = _replace_items_by_indices(field_values[source_field], indices, [indexed_replacement])
            if replaced is not None:
                field_values[source_field] = replaced
                first_id = selected_ids[0] if selected_ids else _make_item_id(source_field, min(indices), indexed_replacement)
                field_item_ids[source_field] = _replace_item_ids_by_indices(field_item_ids[source_field], indices, [first_id])
                return _edit_result(
                    field_values,
                    field_item_ids,
                    {
                        "operation": "edit",
                        "edit_action": "replace_item",
                        "target_field": source_field,
                        "item_indices": indices,
                        "item_ids": [first_id],
                        "replaced_item_ids": selected_ids,
                        "changed": True,
                    },
                )
        return _unsupported_edit_result(today_work, problems, tomorrow_plan, "replace_indices_unresolved", item_ids=field_item_ids)

    text_replacement = _text_replacement_from_text(text)
    if text_replacement is not None:
        old_value, new_value = text_replacement
        replacement_preferred_field = _field_hint_before_edit_operator(text)
        replaced = _replace_unique_text(field_values, replacement_preferred_field, old_value, new_value)
        if replaced:
            replaced_field, replaced_index = replaced
            replaced_id = _item_id_at(field_item_ids, replaced_field, replaced_index)
            return _edit_result(
                field_values,
                field_item_ids,
                {
                    "operation": "edit",
                    "edit_action": "replace_text",
                    "target_field": replaced_field,
                    "item_indices": [replaced_index],
                    "item_ids": [replaced_id] if replaced_id else [],
                    "old_value": old_value,
                    "new_value": new_value,
                    "changed": True,
                },
            )
        return _unsupported_edit_result(today_work, problems, tomorrow_plan, "replace_text_unresolved", item_ids=field_item_ids)

    append_value = _append_value_from_edit_text(text)
    if preferred_field in REPORT_FIELDS and append_value:
        before = list(field_values[preferred_field])
        field_values[preferred_field], field_item_ids[preferred_field], added_ids = _merge_ordered_with_item_ids(
            before,
            field_item_ids[preferred_field],
            [append_value],
            preferred_field,
        )
        return _edit_result(
            field_values,
            field_item_ids,
            {
                "operation": "edit",
                "edit_action": "append_item",
                "target_field": preferred_field,
                "item_count": 1,
                "item_ids": added_ids,
                "changed": before != field_values[preferred_field],
            },
        )

    return _unsupported_edit_result(today_work, problems, tomorrow_plan, "unsupported_edit", item_ids=field_item_ids)


def _parenthetical_delete_edit_result(
    text: str,
    preferred_field: str,
    field_values: dict[str, list[str]],
    item_ids: dict[str, list[str]],
    indices: list[int],
    section_status: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not looks_like_parenthetical_delete(text):
        return None
    target = _resolve_contextual_edit_target(
        text,
        preferred_field,
        field_values,
        section_status,
        indices=indices,
        require_parentheses=True,
    )
    if target is None:
        return _unsupported_edit_result(
            field_values["today_work"],
            field_values["problems"],
            field_values["tomorrow_plan"],
            "parenthetical_delete_target_unresolved",
            item_ids=item_ids,
        )
    field, index = target
    before = field_values[field][index - 1]
    after = _remove_parenthetical_content(before)
    if not after or after == before:
        return _unsupported_edit_result(
            field_values["today_work"],
            field_values["problems"],
            field_values["tomorrow_plan"],
            "parenthetical_delete_no_change",
            item_ids=item_ids,
        )
    field_values[field][index - 1] = after
    item_id = _item_id_at(item_ids, field, index)
    return _edit_result(
        field_values,
        item_ids,
        {
            "operation": "edit",
            "edit_action": "delete_parenthetical_text",
            "target_field": field,
            "item_indices": [index],
            "item_ids": [item_id] if item_id else [],
            "changed": True,
        },
    )


def _spoken_correction_edit_result(
    text: str,
    preferred_field: str,
    field_values: dict[str, list[str]],
    item_ids: dict[str, list[str]],
    indices: list[int],
    section_status: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not looks_like_spoken_correction(text):
        return None
    replacement = _spoken_text_replacement_from_text(text, preferred_field, field_values)
    if replacement is None:
        return _unsupported_edit_result(
            field_values["today_work"],
            field_values["problems"],
            field_values["tomorrow_plan"],
            "spoken_correction_unresolved",
            item_ids=item_ids,
        )
    old_value, new_value = replacement
    target = _resolve_contextual_edit_target(
        text,
        preferred_field,
        field_values,
        section_status,
        indices=indices,
        old_value=old_value,
    )
    if target is None:
        return _unsupported_edit_result(
            field_values["today_work"],
            field_values["problems"],
            field_values["tomorrow_plan"],
            "spoken_correction_target_unresolved",
            item_ids=item_ids,
        )
    field, index = target
    before = field_values[field][index - 1]
    if old_value not in before or old_value == new_value:
        return _unsupported_edit_result(
            field_values["today_work"],
            field_values["problems"],
            field_values["tomorrow_plan"],
            "spoken_correction_no_change",
            item_ids=item_ids,
        )
    field_values[field][index - 1] = before.replace(old_value, new_value, 1)
    item_id = _item_id_at(item_ids, field, index)
    return _edit_result(
        field_values,
        item_ids,
        {
            "operation": "edit",
            "edit_action": "replace_spoken_correction",
            "target_field": field,
            "item_indices": [index],
            "item_ids": [item_id] if item_id else [],
            "old_value": old_value,
            "new_value": new_value,
            "changed": True,
        },
    )


def _negative_replacement_edit_result(
    text: str,
    preferred_field: str,
    field_values: dict[str, list[str]],
    item_ids: dict[str, list[str]],
    indices: list[int],
    section_status: dict[str, Any] | None,
) -> dict[str, Any] | None:
    replacement = _negative_replacement_from_text(text)
    if replacement is None:
        return None
    old_raw, new_raw = replacement
    for old_value in _negative_replacement_old_candidates(old_raw):
        target = _resolve_contextual_edit_target(
            text,
            preferred_field,
            field_values,
            section_status,
            indices=indices,
            old_value=old_value,
        )
        if target is None:
            continue
        field, index = target
        before = field_values[field][index - 1]
        after = _apply_negative_replacement(before, old_value, new_raw)
        if not after or after == before:
            continue
        field_values[field][index - 1] = after
        item_id = _item_id_at(item_ids, field, index)
        return _edit_result(
            field_values,
            item_ids,
            {
                "operation": "edit",
                "edit_action": "replace_negative_correction",
                "target_field": field,
                "item_indices": [index],
                "item_ids": [item_id] if item_id else [],
                "old_value": old_value,
                "new_value": new_raw,
                "changed": True,
            },
        )
    return _unsupported_edit_result(
        field_values["today_work"],
        field_values["problems"],
        field_values["tomorrow_plan"],
        "negative_replacement_target_unresolved",
        item_ids=item_ids,
    )


def _negative_replacement_from_text(text: str) -> tuple[str, str] | None:
    value = str(text or "")
    quantity = _quantity_correction_from_text(value)
    if quantity is not None:
        return quantity
    reverse_match = re.search(r"(?:说错了|错了)[，,。；;]?\s*是\s*(.+?)[，,。；;]?\s*不是\s*(.+)$", value)
    if reverse_match:
        new_value = _clean_edit_value(reverse_match.group(1))
        old_value = _clean_edit_value(reverse_match.group(2))
        if old_value and new_value:
            return old_value, new_value
    normal_match = re.search(r"不是\s*(.+?)[，,。；;]?\s*(?:而是|是)\s*(.+)$", value)
    if normal_match:
        old_value = _clean_edit_value(normal_match.group(1))
        new_value = _clean_edit_value(normal_match.group(2))
        if old_value and new_value:
            return old_value, new_value
    match = re.search(r"不是\s*(.+?)[，,。；;]?\s*(?:而是|是)\s*(.+)$", str(text or ""))
    if not match:
        return None
    old_value = _clean_edit_value(match.group(1))
    new_value = _clean_edit_value(match.group(2))
    if not old_value or not new_value:
        return None
    return old_value, new_value


def _quantity_correction_from_text(text: str) -> tuple[str, str] | None:
    compact = re.sub(r"[\s，,。；;:：]+", "", str(text or ""))
    match = re.search(r"(?:错了|说错了)是([一二两三四五六七八九十0-9]+)份", compact)
    if not match:
        return None
    new_num = match.group(1)
    if "有一份" not in compact or ("昨天" not in compact and "昨日" not in compact):
        return None
    order = ["一", "两", "三", "四", "五", "六", "七", "八", "九", "十"]
    aliases = {"二": "两", "2": "两", "3": "三", "4": "四", "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
    normalized = aliases.get(new_num, new_num)
    if normalized not in order:
        return None
    idx = order.index(normalized)
    if idx + 1 >= len(order):
        return None
    old_num = order[idx + 1]
    return f"{old_num}份", f"{normalized}份"


def _negative_replacement_old_candidates(old_value: str) -> list[str]:
    cleaned = _clean_edit_value(old_value)
    candidates = [cleaned]
    for prefix in ("去", "到", "赴", "前往"):
        if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
            candidates.append(cleaned[len(prefix) :])
    return _dedupe_strings([candidate for candidate in candidates if candidate])


def _apply_negative_replacement(value: str, old_value: str, new_value: str) -> str:
    if old_value not in value:
        return value
    before_old, after_old = value.split(old_value, 1)
    replacement = _clean_edit_value(new_value)
    if replacement.startswith(before_old) and (not after_old or replacement.endswith(after_old)):
        return replacement
    if after_old and replacement.endswith(after_old):
        replacement = _clean_edit_value(replacement[: -len(after_old)])
    if not replacement:
        return value
    return f"{before_old}{replacement}{after_old}"


def _resolve_contextual_edit_target(
    text: str,
    preferred_field: str,
    field_values: dict[str, list[str]],
    section_status: dict[str, Any] | None,
    *,
    indices: list[int],
    require_parentheses: bool = False,
    old_value: str = "",
) -> tuple[str, int] | None:
    if len(indices) == 1:
        source_field = _source_field_for_indices(preferred_field, field_values, indices)
        if source_field and _contextual_target_score(
            field_values[source_field][indices[0] - 1],
            _contextual_content_hints(text),
            require_parentheses=require_parentheses,
            old_value=old_value,
        ) >= 0:
            return source_field, indices[0]

    status_target = _last_modified_item_target(section_status)
    if status_target is not None:
        field, index = status_target
        if (preferred_field not in REPORT_FIELDS or preferred_field == field) and 1 <= index <= len(field_values.get(field, [])):
            if _contextual_target_score(
                field_values[field][index - 1],
                _contextual_content_hints(text),
                require_parentheses=require_parentheses,
                old_value=old_value,
            ) >= 0:
                return field, index

    fields = [preferred_field] if preferred_field in REPORT_FIELDS else list(REPORT_FIELD_ORDER)
    hints = _contextual_content_hints(text)
    matches: list[tuple[int, str, int]] = []
    for field in fields:
        for index, value in enumerate(field_values[field], start=1):
            score = _contextual_target_score(
                value,
                hints,
                require_parentheses=require_parentheses,
                old_value=old_value,
            )
            if score >= 0:
                matches.append((score, field, index))
    if not matches:
        return None
    best = max(score for score, _, _ in matches)
    best_matches = [(field, index) for score, field, index in matches if score == best]
    if len(best_matches) == 1:
        return best_matches[0]
    if len(matches) == 1:
        _, field, index = matches[0]
        return field, index
    return None


def _contextual_target_score(
    value: str,
    hints: list[str],
    *,
    require_parentheses: bool,
    old_value: str,
) -> int:
    text = str(value or "")
    if require_parentheses and not _has_parenthetical_content(text):
        return -1
    if old_value and old_value not in text:
        return -1
    score = 0
    if require_parentheses:
        score += 2
    if old_value:
        score += 10
    if hints and any(hint in text for hint in hints):
        score += 5
    return score


def _contextual_content_hints(text: str) -> list[str]:
    head = re.split(r"(?:是对的|写错了?|打错了?|识别错了?|听错了?|记错了?|错了|括号|内容|删掉|删除|去掉|清掉)", str(text or ""), maxsplit=1)[0]
    head = _strip_daily_context_words(head)
    tokens = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,16}", head)
    result: list[str] = []
    for token in tokens:
        value = _clean_edit_value(token)
        if value and value not in {"今日工作", "今天工作", "明日计划", "明天计划", "问题风险", "日报草稿"}:
            result.append(value)
    return _dedupe_strings(result)


def _spoken_text_replacement_from_text(
    text: str,
    preferred_field: str,
    field_values: dict[str, list[str]],
) -> tuple[str, str] | None:
    match = re.search(r"(.+?)(?:写错了?|打错了?|识别错了?|听错了?|记错了?|错了)[，,。；;]?\s*(.+)$", str(text or ""))
    if not match:
        return None
    head = match.group(1)
    tail = match.group(2)
    new_value = _spoken_replacement_from_tail(tail)
    if not new_value:
        return None
    old_value = _spoken_old_value_from_head(head, new_value, preferred_field, field_values)
    if not old_value:
        return None
    return old_value, new_value


def _spoken_replacement_from_tail(tail: str) -> str:
    chars = re.findall(r"的([\u4e00-\u9fa5A-Za-z0-9])", str(tail or ""))
    if chars:
        return _clean_edit_value("".join(chars[:6]))
    match = re.search(r"(?:应该是|改成|改为|替换成|替换为|是)\s*([\u4e00-\u9fa5A-Za-z0-9]{1,8})", str(tail or ""))
    if match:
        return _clean_edit_value(match.group(1))
    return ""


def _spoken_old_value_from_head(
    head: str,
    new_value: str,
    preferred_field: str,
    field_values: dict[str, list[str]],
) -> str:
    source = _compact(_strip_daily_context_words(head))
    if not source:
        return ""
    lengths: list[int] = []
    if new_value:
        lengths.append(len(_compact(new_value)))
    lengths.extend(range(min(6, len(source)), 0, -1))
    fields = [preferred_field] if preferred_field in REPORT_FIELDS else list(REPORT_FIELD_ORDER)
    seen_lengths: set[int] = set()
    for length in lengths:
        if length < 1 or length in seen_lengths:
            continue
        seen_lengths.add(length)
        for start in range(len(source) - length, -1, -1):
            candidate = source[start : start + length]
            if not _valid_spoken_old_candidate(candidate):
                continue
            if _unique_term_occurrence(candidate, fields, field_values):
                return candidate
    return ""


def _unique_term_occurrence(term: str, fields: list[str], field_values: dict[str, list[str]]) -> bool:
    matches = 0
    for field in fields:
        for value in field_values[field]:
            if term in value:
                matches += 1
                if matches > 1:
                    return False
    return matches == 1


def _valid_spoken_old_candidate(candidate: str) -> bool:
    if not candidate:
        return False
    return candidate not in {
        "日报",
        "日志",
        "草稿",
        "今日",
        "今天",
        "明日",
        "明天",
        "计划",
        "工作",
        "问题",
        "风险",
        "里面",
        "里的",
    }


def _strip_daily_context_words(text: str) -> str:
    value = str(text or "")
    for marker in (
        "今日工作",
        "今天工作",
        "今日事项",
        "工作内容",
        "问题/风险",
        "问题风险",
        "风险问题",
        "明日计划",
        "明天计划",
        "明日工作",
        "明天工作",
        "日报",
        "日志",
        "草稿",
        "里面的",
        "里的",
        "里面",
        "中",
        "第",
        "条",
        "项",
    ):
        value = value.replace(marker, "")
    return value


def _has_parenthetical_content(value: str) -> bool:
    return bool(re.search(r"[（(][^（）()]+[）)]", str(value or "")))


def _remove_parenthetical_content(value: str) -> str:
    text = re.sub(r"[（(][^（）()]+[）)]", "", str(value or ""))
    return _clean_edit_value(text)


def _edit_result(field_values: dict[str, list[str]], item_ids: dict[str, list[str]], action: dict[str, Any]) -> dict[str, Any]:
    return {
        "today_work": field_values["today_work"],
        "problems": field_values["problems"],
        "tomorrow_plan": field_values["tomorrow_plan"],
        "item_ids": _normalize_item_ids_for_values(item_ids, field_values),
        "action": action,
    }


def _unsupported_edit_result(
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    reason: str,
    *,
    item_ids: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    field_values = {"today_work": list(today_work), "problems": list(problems), "tomorrow_plan": list(tomorrow_plan)}
    return {
        "today_work": list(today_work),
        "problems": list(problems),
        "tomorrow_plan": list(tomorrow_plan),
        "item_ids": _normalize_item_ids_for_values(item_ids, field_values),
        "action": {"operation": "edit", "changed": False, "reason": reason},
    }


def _empty_item_ids() -> dict[str, list[str]]:
    return {field: [] for field in REPORT_FIELD_ORDER}


def _make_item_ids_for_values(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> dict[str, list[str]]:
    return _normalize_item_ids_for_values(
        None,
        {"today_work": today_work, "problems": problems, "tomorrow_plan": tomorrow_plan},
    )


def _item_ids_from_section_status(
    section_status: dict[str, Any] | None,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> dict[str, list[str]]:
    raw = _draft_item_ids_from_status(section_status)
    return _normalize_item_ids_for_values(
        raw,
        {"today_work": today_work, "problems": problems, "tomorrow_plan": tomorrow_plan},
    )


def _normalize_item_ids_for_values(
    item_ids: dict[str, list[str]] | None,
    field_values: dict[str, list[str]],
) -> dict[str, list[str]]:
    raw = item_ids if isinstance(item_ids, dict) else {}
    result: dict[str, list[str]] = {}
    for field in REPORT_FIELD_ORDER:
        values = list(field_values.get(field, []) or [])
        ids = list(raw.get(field, []) or [])
        used: set[str] = set()
        normalized: list[str] = []
        for index, value in enumerate(values, start=1):
            candidate = str(ids[index - 1]).strip() if index - 1 < len(ids) else ""
            if not candidate or candidate in used:
                candidate = _make_unique_item_id(field, index, value, used)
            normalized.append(candidate)
            used.add(candidate)
        result[field] = normalized
    return result


def _make_unique_item_id(field: str, index: int, value: str, used: set[str]) -> str:
    candidate = _make_item_id(field, index, value)
    if candidate not in used:
        return candidate
    attempt = 2
    while True:
        digest = hashlib.sha1(f"{field}:{index}:{value}:{attempt}".encode("utf-8")).hexdigest()[:12]
        candidate = f"di_{digest}"
        if candidate not in used:
            return candidate
        attempt += 1


def _merge_ordered_with_item_ids(
    existing_values: list[str],
    existing_ids: list[str],
    incoming: list[str],
    field: str,
    *,
    incoming_ids: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    result_values = list(existing_values or [])
    result_ids = _normalize_item_ids_for_values(
        {field: list(existing_ids or [])},
        {field: result_values, **{other: [] for other in REPORT_FIELD_ORDER if other != field}},
    )[field]
    seen = {item.strip() for item in result_values if item.strip()}
    used = set(result_ids)
    added_ids: list[str] = []
    source_ids = list(incoming_ids or [])
    for offset, item in enumerate(incoming):
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        duplicate_index = _semantic_duplicate_index(result_values, value, field)
        if duplicate_index is not None:
            if _should_replace_semantic_duplicate(result_values[duplicate_index], value, field):
                result_values[duplicate_index] = value
                item_id = result_ids[duplicate_index] if duplicate_index < len(result_ids) else ""
                if not item_id:
                    item_id = _make_unique_item_id(field, duplicate_index + 1, value, used)
                    if duplicate_index < len(result_ids):
                        result_ids[duplicate_index] = item_id
                    else:
                        result_ids.append(item_id)
                added_ids.append(item_id)
                seen.add(value)
            continue
        item_id = str(source_ids[offset]).strip() if offset < len(source_ids) else ""
        if not item_id or item_id in used:
            item_id = _make_unique_item_id(field, len(result_values) + 1, value, used)
        result_values.append(value)
        result_ids.append(item_id)
        added_ids.append(item_id)
        seen.add(value)
        used.add(item_id)
    return result_values, result_ids, added_ids


def _semantic_duplicate_index(existing_values: list[str], incoming: str, field: str) -> int | None:
    if field != "tomorrow_plan":
        return None
    incoming_key = _tomorrow_trip_key(incoming)
    if not incoming_key:
        return None
    for index, existing in enumerate(existing_values):
        if _tomorrow_trip_key(existing) == incoming_key:
            return index
    return None


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _field_already_contains_all(existing_values: list[str], incoming_values: list[str]) -> bool:
    incoming = [str(value or "").strip() for value in incoming_values if str(value or "").strip()]
    if not incoming:
        return True
    existing = {str(value or "").strip() for value in existing_values if str(value or "").strip()}
    return all(value in existing for value in incoming)


def _should_replace_semantic_duplicate(existing: str, incoming: str, field: str) -> bool:
    if field != "tomorrow_plan":
        return False
    return _specificity_score(incoming) > _specificity_score(existing)


def _tomorrow_trip_key(value: str) -> str:
    text = str(value or "")
    if not _contains_any(text, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")):
        return ""
    destination = _trip_destination_hint(text)
    if not destination:
        return ""
    if not _contains_any(text, ("\u51fa\u5dee", "\u53bb", "\u8d74", "\u5230", "\u5f00\u5ead", "\u76d6\u7ae0", "\u8d70\u8bbf", "\u6c9f\u901a", "\u529e\u7406")):
        return ""
    return f"tomorrow_trip:{destination}"


def _trip_destination_hint(value: str) -> str:
    text = str(value or "")
    for destination in (
        "\u5609\u5174\u5357\u6e56\u8857\u9053",
        "\u5357\u6e56\u8857\u9053",
        "\u5357\u4eac",
        "\u4e09\u4e9a",
        "\u626c\u5dde",
        "\u5609\u5174",
        "\u5e38\u5dde",
        "\u82cf\u5dde",
        "\u4e0a\u6d77",
        "\u5317\u4eac",
        "\u5e7f\u5dde",
        "\u6df1\u5733",
    ):
        if destination in text:
            return destination
    match = re.search(
        r"(?:\u51fa\u5dee|\u53bb|\u8d74|\u5230)([\u4e00-\u9fa5]{2,8}?)(?:\u529e\u7406|\u5f00\u5ead|\u76d6\u7ae0|\u8d70\u8bbf|\u5904\u7406|\u6c9f\u901a|\u8ba8\u85aa|\u51fa\u5dee|$)",
        text,
    )
    if not match:
        return ""
    candidate = match.group(1).strip()
    if _contains_any(candidate, ("\u6848", "\u6848\u4ef6", "\u5f00\u5ead", "\u6c9f\u901a", "\u5904\u7406", "\u529e\u7406")):
        return ""
    return candidate


def _specificity_score(value: str) -> int:
    text = str(value or "")
    score = len(_compact(text))
    for marker in ("\u529e\u7406", "\u5f00\u5ead", "\u6c9f\u901a", "\u76d6\u7ae0", "\u6848\u4ef6", "\u6848", "\u6cd5\u9662", "\u4e2d\u9662"):
        if marker in text:
            score += 8
    return score


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _replace_item_ids_by_indices(values: list[str], indices: list[int], replacement: list[str]) -> list[str] | None:
    if not _indices_in_values(values, indices) or not replacement:
        return None
    updated = list(values)
    first = min(indices)
    for index in sorted(set(indices), reverse=True):
        updated.pop(index - 1)
    for offset, item in enumerate(replacement):
        updated.insert(first - 1 + offset, item)
    return updated


def _merge_item_ids_by_indices(values: list[str], indices: list[int], merged_id: str) -> list[str]:
    if len(indices) < 2 or not _indices_in_values(values, indices):
        return list(values)
    selected = set(indices)
    first = min(indices)
    updated: list[str] = []
    for position, item in enumerate(values, start=1):
        if position == first:
            updated.append(merged_id)
        if position in selected:
            continue
        updated.append(item)
    return updated


def _item_id_at(item_ids: dict[str, list[str]], field: str, index: int) -> str:
    values = item_ids.get(field, [])
    if 1 <= index <= len(values):
        return values[index - 1]
    return ""


def _indices_for_item_ids(values: list[str], target_ids: list[str]) -> list[int]:
    targets = {value for value in target_ids if value}
    return [index for index, item_id in enumerate(values, start=1) if item_id in targets]


def _all_item_ids(item_ids: dict[str, list[str]]) -> list[str]:
    result: list[str] = []
    for field in REPORT_FIELD_ORDER:
        result.extend(str(item_id) for item_id in item_ids.get(field, []) if str(item_id).strip())
    return result


def _resolve_item_indices_for_edit(
    text: str,
    preferred_field: str,
    field_values: dict[str, list[str]],
    section_status: dict[str, Any] | None,
) -> tuple[str, list[int]]:
    explicit = _edit_item_indices_from_text(text)
    if explicit:
        explicit_field = _field_from_text(text)
        if explicit_field in REPORT_FIELDS:
            return explicit_field, explicit
        global_target = _global_field_indices_for_indices(field_values, explicit)
        if global_target is not None:
            return global_target
        return preferred_field, explicit
    recent_target = _recent_item_target_from_text(text, preferred_field, field_values, section_status)
    if recent_target is None:
        return "", []
    return recent_target[0], [recent_target[1]]


def _recent_item_target_from_text(
    text: str,
    preferred_field: str,
    field_values: dict[str, list[str]],
    section_status: dict[str, Any] | None,
) -> tuple[str, int] | None:
    if not _has_recent_item_reference(text):
        return None
    status_target = _last_modified_item_target(section_status)
    if status_target is not None:
        field, index = status_target
        if field in REPORT_FIELDS and 1 <= index <= len(field_values.get(field, [])):
            return field, index
    if _is_bare_short_delete_reference(text) or _has_deictic_item_reference(text):
        return None
    if preferred_field in REPORT_FIELDS and field_values.get(preferred_field):
        return preferred_field, len(field_values[preferred_field])
    candidates = [field for field in REPORT_FIELD_ORDER if field_values.get(field)]
    if len(candidates) == 1:
        field = candidates[0]
        return field, len(field_values[field])
    if field_values.get("today_work"):
        return "today_work", len(field_values["today_work"])
    return None


def _has_deictic_item_reference(text: str) -> bool:
    compact = _compact(text)
    return any(token in compact for token in ("刚才那条", "刚才那个", "刚刚那条", "刚刚那个", "这条", "那条", "这个", "那个", "上一条", "上条", "它"))


def _has_recent_item_reference(text: str) -> bool:
    compact = _compact(text)
    return any(
        token in compact
        for token in (
            "刚才那条",
            "刚才那个",
            "刚刚那条",
            "刚刚那个",
            "上一条",
            "上条",
            "最后一条",
            "最后一个",
            "刚填的",
            "刚写的",
            "刚记录的",
            "这条",
            "那条",
            "删掉",
            "删除",
            "删了",
            "删一下",
            "删下",
        )
    )


def _is_bare_short_delete_reference(text: str) -> bool:
    compact = _compact(text)
    if not any(token in compact for token in ("删掉", "删除", "删了", "删一下", "删下")):
        return False
    if any(
        token in compact
        for token in (
            "刚才",
            "刚刚",
            "这条",
            "那条",
            "这个",
            "那个",
            "上一条",
            "上条",
            "最后一条",
            "它",
        )
    ):
        return False
    return len(compact) <= 5


def _last_modified_item_target(section_status: dict[str, Any] | None) -> tuple[str, int] | None:
    reference = focus_item(section_status)
    return reference.target_tuple() if reference is not None else None


def _command_text(command: DailyCommand) -> str:
    for item in command.content:
        value = str(item or "").strip()
        if value:
            return value
    return ""


def _field_from_text(text: str) -> str:
    compact = _compact(text)
    if any(marker in compact for marker in ("今日工作", "今天工作", "今日事项", "工作内容")):
        return "today_work"
    if any(marker in compact for marker in ("问题风险", "问题/风险", "风险问题", "问题", "风险")):
        return "problems"
    if any(marker in compact for marker in ("明日计划", "明天计划", "明日工作", "明天工作", "计划")):
        return "tomorrow_plan"
    return ""


def _focused_candidate_edit_result_from_text(
    text: str,
    field_values: dict[str, list[str]],
    item_ids: dict[str, list[str]],
    section_status: dict[str, Any] | None,
) -> dict[str, Any] | None:
    target = _pending_candidate_target(section_status, field_values, item_ids)
    if target is None:
        return None
    field, index = target
    before = field_values[field][index - 1]
    replacement = _focused_candidate_full_replacement_from_text(text)
    item_id = _item_id_at(item_ids, field, index)
    if replacement:
        field_values[field][index - 1] = replacement
        return _edit_result(
            field_values,
            item_ids,
            {
                "operation": "edit",
                "edit_action": "replace_item",
                "target_field": field,
                "item_indices": [index],
                "target_item_indices": [index],
                "item_ids": [item_id] if item_id else [],
                "replaced_item_ids": [item_id] if item_id else [],
                "changed": before != replacement,
                "source": "pending_daily_candidate",
            },
        )
    text_replacement = _focused_candidate_text_replacement_from_text(text)
    if text_replacement is None:
        return None
    old_value, new_value = text_replacement
    if not old_value or old_value not in before or old_value == new_value:
        return None
    field_values[field][index - 1] = before.replace(old_value, new_value, 1)
    return _edit_result(
        field_values,
        item_ids,
        {
            "operation": "edit",
            "edit_action": "replace_text",
            "target_field": field,
            "item_indices": [index],
            "item_ids": [item_id] if item_id else [],
            "old_value": old_value,
            "new_value": new_value,
            "changed": True,
            "source": "pending_daily_candidate",
        },
    )


def _pending_candidate_target(
    section_status: dict[str, Any] | None,
    field_values: dict[str, list[str]],
    item_ids: dict[str, list[str]],
) -> tuple[str, int] | None:
    reference = pending_daily_candidate(section_status)
    if reference is None:
        return None
    field = reference.field
    expected_id = reference.item_id
    if expected_id and expected_id in item_ids.get(field, []):
        return field, item_ids[field].index(expected_id) + 1
    index = reference.item_index
    if index < 1 or index > len(field_values.get(field, [])):
        return None
    expected_text = reference.text
    if expected_text and field_values[field][index - 1] != expected_text:
        return None
    return field, index


def _focused_candidate_full_replacement_from_text(text: str) -> str:
    value = str(text or "").strip()
    patterns = (
        r"^(?:对[，,]?\s*)?(?:就这个|就是这个|就这条|这条|那条|这个|那个|它)?\s*(?:改成|改为|修改为|替换为|替换成|换成|换为|更新为|变成)\s*(.+)$",
        r"^把\s*(?:就这个|就是这个|就这条|这条|那条|这个|那个|它)\s*(?:改成|改为|修改为|替换为|替换成|换成|换为|更新为|变成)\s*(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        replacement = _clean_edit_value(match.group(1))
        if replacement:
            return replacement
    return ""


def _focused_candidate_text_replacement_from_text(text: str) -> tuple[str, str] | None:
    replacement = _text_replacement_from_text(text)
    if replacement is None:
        return None
    old_value, new_value = replacement
    old_value = _strip_candidate_reference_words(old_value)
    if not old_value or not new_value or old_value in {"这条", "那条", "这个", "那个", "它", "里面", "里的"}:
        return None
    return old_value, new_value


def _strip_candidate_reference_words(text: str) -> str:
    value = _clean_edit_value(text)
    for prefix in (
        "这条里面的",
        "那条里面的",
        "它里面的",
        "这里面的",
        "那里面的",
        "这条里的",
        "那条里的",
        "它里的",
        "里面的",
        "里的",
        "这条中",
        "那条中",
        "这条",
        "那条",
        "这个",
        "那个",
        "它",
    ):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    return _clean_edit_value(value)


def _field_replacement_from_text(text: str) -> tuple[str, str] | None:
    for field, label_pattern in (
        ("today_work", r"(?:今日工作|今天工作|今日事项|工作内容)"),
        ("problems", r"(?:问题/风险|问题风险|风险问题|问题|风险)"),
        ("tomorrow_plan", r"(?:明日计划|明天计划|明日工作|明天工作|计划)"),
    ):
        match = re.search(label_pattern + r"\s*(?:改成|改为|修改为|替换为|换成|换为|更新为|变成|是)\s*(.+)$", text)
        if match:
            value = _clean_edit_value(match.group(1))
            if value:
                return field, value
    return None


def _append_value_from_edit_text(text: str) -> str:
    match = re.search(
        r"(?:今日工作|今天工作|今日事项|工作内容|问题/风险|问题风险|风险问题|问题|风险|明日计划|明天计划|明日工作|明天工作|计划)?\s*(?:增加|新增|补充|加上|加入)\s*(.+)$",
        text,
    )
    if not match:
        return ""
    return _clean_edit_value(match.group(1))


def _delete_append_edit_from_text(text: str) -> tuple[str, str] | None:
    for pattern in (
        r"把\s*(.+?)\s*(?:去掉|删掉|删除|移除)\s*(?:，|,|。|；|;|\s)*(?:再)?(?:加上|加入|补上|追加)\s*(.+)$",
        r"(?:去掉|删掉|删除|移除)\s*(.+?)\s*(?:，|,|。|；|;|\s)*(?:再)?(?:加上|加入|补上|追加)\s*(.+)$",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        delete_value = _clean_edit_value(match.group(1))
        append_value = _clean_edit_value(match.group(2))
        if delete_value and append_value:
            return delete_value, append_value
    return None


def _indexed_replacement_from_text(text: str) -> str:
    match = re.search(r"(?:改成|改为|修改为|替换为|换成|换为|更新为|变成)\s*(.+)$", text)
    if not match:
        return ""
    return _clean_edit_value(match.group(1))


def _text_replacement_from_text(text: str) -> tuple[str, str] | None:
    for pattern in (
        r"把\s*(.+?)\s*(?:改成|改为|替换成|替换为|换成|换为|更新为|修改为)\s*(.+)$",
        r"(.+?)\s*(?:改成|改为|替换成|替换为|换成|换为|更新为|修改为)\s*(.+)$",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        old_value = _clean_edit_value(match.group(1))
        new_value = _clean_edit_value(match.group(2))
        if not old_value or not new_value:
            continue
        if _item_indices_from_text(old_value) or _field_from_text(old_value):
            continue
        return old_value, new_value
    return None


def _field_hint_before_edit_operator(text: str) -> str:
    head = re.split(r"(?:改成|改为|替换成|替换为|换成|换为|更新为|修改为|变成)", str(text or ""), maxsplit=1)[0]
    return _field_from_text(head)


def _item_indices_from_text(text: str) -> list[int]:
    result: list[int] = []
    range_pattern = re.compile(
        r"第?\s*([0-9一二三四五六七八九十两]+)\s*(?:到|至|-|—|~)\s*第?\s*([0-9一二三四五六七八九十两]+)\s*[条项]?"
    )
    consumed: list[tuple[int, int]] = []
    for match in range_pattern.finditer(text):
        start = _ordinal_to_int(match.group(1))
        end = _ordinal_to_int(match.group(2))
        if start and end:
            low, high = sorted((start, end))
            result.extend(range(low, high + 1))
            consumed.append(match.span())
    masked = list(text)
    for start, end in consumed:
        for index in range(start, end):
            masked[index] = " "
    single_pattern = re.compile(r"第?\s*([0-9一二三四五六七八九十两]+)\s*[条项]")
    for match in single_pattern.finditer("".join(masked)):
        value = _ordinal_to_int(match.group(1))
        if value:
            result.append(value)
    return sorted(dict.fromkeys(index for index in result if index > 0))


def _edit_item_indices_from_text(text: str) -> list[int]:
    result = list(_item_indices_from_text(text))
    result.extend(_loose_item_indices_from_text(text))
    return sorted(dict.fromkeys(index for index in result if index > 0))


def _loose_item_indices_from_text(text: str) -> list[int]:
    if not _contains_structural_edit_intent(text):
        return []
    raw = str(text or "")
    result: list[int] = []
    ordinal = r"[0-9一二三四五六七八九十两]+"

    for match in re.finditer(rf"{ordinal}(?:\s*[、,，.．]\s*{ordinal})+", raw):
        if not _near_structural_marker(raw, match.span()):
            continue
        for token in re.findall(ordinal, match.group(0)):
            value = _ordinal_to_int(token)
            if value:
                result.append(value)

    field_label = r"(?:今日工作|今天工作|今日事项|工作内容|问题/风险|问题风险|风险问题|问题|风险|明日计划|明天计划|明日工作|明天工作|计划)"
    for match in re.finditer(rf"{field_label}\s*({ordinal})\s*[.．、]", raw):
        value = _ordinal_to_int(match.group(1))
        if value and _near_structural_marker(raw, match.span()):
            result.append(value)

    for match in re.finditer(rf"(?<![0-9])({ordinal})\s*[.．](?=\s*(?:改成|改为|修改|替换|换成|换为|删除|删掉|去掉|合并|并成|并为|归并))", raw):
        value = _ordinal_to_int(match.group(1))
        if value and _near_structural_marker(raw, match.span()):
            result.append(value)

    compact = _compact(raw)
    adjacent_patterns = (
        r"([1-9]{2,})(?=(?:合并|并成|并为|归并|删除|删掉|去掉))",
        r"(?:合并|并成|并为|归并|删除|删掉|去掉)([1-9]{2,})",
        r"([一二三四五六七八九两]{2,})(?=(?:合并|并成|并为|归并|删除|删掉|去掉))",
        r"(?:合并|并成|并为|归并|删除|删掉|去掉)([一二三四五六七八九两]{2,})",
    )
    for pattern in adjacent_patterns:
        for match in re.finditer(pattern, compact):
            token = match.group(1)
            if token.isdigit():
                result.extend(int(char) for char in token)
            else:
                for char in token:
                    value = _ordinal_to_int(char)
                    if value:
                        result.append(value)

    return sorted(dict.fromkeys(index for index in result if index > 0))


def _near_structural_marker(text: str, span: tuple[int, int]) -> bool:
    start, end = span
    window = text[max(0, start - 12) : min(len(text), end + 12)]
    return bool(
        _field_from_text(window)
        or any(
            marker in window
            for marker in (
                "改成",
                "改为",
                "修改",
                "替换",
                "换成",
                "换为",
                "变成",
                "更新为",
                "删除",
                "删掉",
                "去掉",
                "合并",
                "并成",
                "并为",
                "归并",
                "移到",
                "移入",
                "放到",
                "放进",
                "挪到",
                "转到",
                "归到",
            )
        )
    )


def _global_field_indices_for_indices(
    field_values: dict[str, list[str]],
    indices: list[int],
) -> tuple[str, list[int]] | None:
    mapped: list[tuple[str, int]] = []
    for index in indices:
        remaining = index
        target: tuple[str, int] | None = None
        for field in REPORT_FIELD_ORDER:
            count = len(field_values.get(field, []) or [])
            if 1 <= remaining <= count:
                target = (field, remaining)
                break
            remaining -= count
        if target is None:
            return None
        mapped.append(target)
    fields = {field for field, _ in mapped}
    if len(fields) != 1:
        return None
    field = mapped[0][0]
    return field, [index for _, index in mapped]


def _ordinal_to_int(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _has_delete_item_intent(text: str) -> bool:
    compact = _compact(text)
    return any(token in compact for token in ("删除", "删掉", "去掉", "删了", "删除掉"))


def _looks_like_field_clear(text: str, preferred_field: str) -> bool:
    if preferred_field not in REPORT_FIELDS:
        return False
    compact = _compact(text)
    if not compact or _edit_item_indices_from_text(text):
        return False
    field_markers = {
        "today_work": ("把今日工作", "把今天工作", "把今日事项", "把工作内容", "今日工作", "今天工作", "今日事项", "工作内容"),
        "problems": ("把问题/风险", "把问题风险", "把风险问题", "把问题", "把风险", "问题/风险", "问题风险", "风险问题"),
        "tomorrow_plan": ("把明日计划", "把明天计划", "把明日工作", "把明天工作", "明日计划", "明天计划", "明日工作", "明天工作"),
    }
    delete_markers = ("删除", "删掉", "去掉", "删了", "删除掉", "清空")
    return any(marker in compact for marker in field_markers[preferred_field]) and any(marker in compact for marker in delete_markers)


def _has_merge_intent(text: str) -> bool:
    compact = _compact(text)
    return any(token in compact for token in ("合并", "并成", "并为", "归并"))


def _has_move_intent(text: str) -> bool:
    compact = _compact(text)
    return any(token in compact for token in ("移到", "移入", "放到", "放进", "挪到", "转到", "归到"))


def _contains_structural_edit_intent(text: str) -> bool:
    compact = _compact(text)
    return any(
        token in compact
        for token in (
            "改成",
            "改为",
            "修改",
            "替换",
            "换成",
            "换为",
            "更新为",
            "变成",
            "删除",
            "删掉",
            "去掉",
            "合并",
            "并成",
            "移到",
            "移入",
            "放到",
            "放进",
            "挪到",
            "转到",
            "归到",
        )
    )


def _move_destination_field(text: str, source_field: str) -> str:
    for marker in ("移到", "移入", "放到", "放进", "挪到", "转到", "归到"):
        if marker in text:
            destination = _field_from_text(text.split(marker, 1)[1])
            if destination:
                return destination
    destination = _field_from_text(text)
    if destination and destination != source_field:
        return destination
    return ""


def _source_field_for_indices(preferred_field: str, field_values: dict[str, list[str]], indices: list[int]) -> str:
    if preferred_field in REPORT_FIELDS and _indices_in_values(field_values[preferred_field], indices):
        return preferred_field
    candidates = [field for field in REPORT_FIELD_ORDER if _indices_in_values(field_values[field], indices)]
    if len(candidates) == 1:
        return candidates[0]
    if "today_work" in candidates:
        return "today_work"
    return ""


def _indices_in_values(values: list[str], indices: list[int]) -> bool:
    return bool(indices) and all(1 <= index <= len(values) for index in indices)


def _select_by_indices(values: list[str], indices: list[int]) -> list[str]:
    return [values[index - 1] for index in indices if 1 <= index <= len(values)]


def _delete_by_indices(values: list[str], indices: list[int]) -> list[str]:
    selected = {index for index in indices if 1 <= index <= len(values)}
    return [value for position, value in enumerate(values, start=1) if position not in selected]


def _replace_items_by_indices(values: list[str], indices: list[int], replacement: list[str]) -> list[str] | None:
    if not _indices_in_values(values, indices) or not replacement:
        return None
    updated = list(values)
    first = min(indices)
    for index in sorted(set(indices), reverse=True):
        updated.pop(index - 1)
    for offset, item in enumerate(replacement):
        updated.insert(first - 1 + offset, item)
    return updated


def _merge_items_by_indices(values: list[str], indices: list[int]) -> list[str] | None:
    if len(indices) < 2 or not _indices_in_values(values, indices):
        return None
    merged_value = "，".join(str(values[index - 1]).strip() for index in indices if str(values[index - 1]).strip())
    merged_value = merged_value.strip(" ，,。；;、")
    if not merged_value:
        return None
    selected = set(indices)
    first = min(indices)
    updated: list[str] = []
    for position, item in enumerate(values, start=1):
        if position == first:
            updated.append(merged_value)
        if position in selected:
            continue
        updated.append(item)
    return updated


def _replace_unique_text(field_values: dict[str, list[str]], preferred_field: str, old_value: str, new_value: str) -> tuple[str, int] | None:
    fields = [preferred_field] if preferred_field in REPORT_FIELDS else list(REPORT_FIELD_ORDER)
    matches: list[tuple[str, int]] = []
    for field in fields:
        for index, value in enumerate(field_values[field]):
            if old_value in value:
                matches.append((field, index))
    if len(matches) != 1:
        return None
    field, index = matches[0]
    field_values[field][index] = field_values[field][index].replace(old_value, new_value, 1)
    return field, index + 1


def _field_for_unique_or_preferred_text(field_values: dict[str, list[str]], preferred_field: str, value: str) -> str:
    if preferred_field in REPORT_FIELDS and any(value in item for item in field_values[preferred_field]):
        return preferred_field
    matching_fields = [
        field
        for field in REPORT_FIELD_ORDER
        if any(value in item for item in field_values[field])
    ]
    if len(matching_fields) == 1:
        return matching_fields[0]
    if "today_work" in matching_fields:
        return "today_work"
    return ""


def _clean_edit_value(value: str) -> str:
    return str(value or "").strip().strip(" ：:，,。；;、\"'“”‘’")


def _values_for_field(today_work: list[str], problems: list[str], tomorrow_plan: list[str], field: str) -> list[str]:
    if field == "today_work":
        return today_work
    if field == "problems":
        return problems
    if field == "tomorrow_plan":
        return tomorrow_plan
    return []


def _set_field_values(
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    field: str,
    values: list[str],
) -> tuple[list[str], list[str], list[str]]:
    if field == "today_work":
        return list(values), problems, tomorrow_plan
    if field == "problems":
        return today_work, list(values), tomorrow_plan
    if field == "tomorrow_plan":
        return today_work, problems, list(values)
    return today_work, problems, tomorrow_plan


async def _previous_report_for_copy(
    session: "AsyncSession",
    *,
    user: "User",
    report_date: date,
    commands: list[DailyCommand],
) -> "DailyReport | None":
    from app.repositories import get_report

    if not any(command.operation in {"copy_previous", "complete_previous_plan"} for command in commands):
        return None
    offset = 1
    for command in commands:
        if command.operation == "copy_previous":
            offset = _relative_date_offset(command.target_date) or 1
            break
        if command.operation == "complete_previous_plan":
            offset = 1
            break
    return await get_report(session, user.id, report_date - timedelta(days=offset))


def _report_date_for_commands(base_report_date: date, commands: list[DailyCommand]) -> date:
    for command in commands:
        absolute = _absolute_report_date(command.target_date)
        if absolute:
            return absolute
        offset = _relative_date_offset(command.target_date)
        if offset and command.operation in {"fill", "edit", "query_history", "begin_edit", "clear", "revoke"}:
            return base_report_date - timedelta(days=offset)
    for command in commands:
        if command.target_date in {"today", "tomorrow"}:
            continue
        active_report_date = _absolute_report_date(command.active_report_date)
        if active_report_date and command.operation in {
            "fill",
            "edit",
            "confirm",
            "query_current",
            "query_history",
            "begin_edit",
            "clear",
            "revoke",
            "copy_previous",
            "complete_previous_plan",
        }:
            return active_report_date
    return base_report_date


def _should_block_historical_daily_mutation_after_cutoff(raw_input: str, received_at: datetime) -> bool:
    if received_at.hour < 9:
        return False
    text = _compact_text(raw_input)
    if not text:
        return False
    if not _has_historical_day_reference(text):
        return False
    if _has_safe_historical_copy_reference(text):
        return False
    return _has_historical_mutation_marker(text)


def _historical_report_date_from_raw_input(base_report_date: date, raw_input: str) -> date:
    text = _compact_text(raw_input)
    if "\u5927\u524d\u5929" in text:
        return base_report_date - timedelta(days=3)
    if "\u524d\u5929" in text:
        return base_report_date - timedelta(days=2)
    return base_report_date - timedelta(days=1)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _has_historical_day_reference(text: str) -> bool:
    return any(marker in text for marker in ("\u6628\u5929", "\u6628\u65e5", "\u6628\u513f", "\u6628\u4e2a", "\u524d\u5929", "\u5927\u524d\u5929"))


def _has_safe_historical_copy_reference(text: str) -> bool:
    safe_markers = (
        "\u590d\u5236",
        "copy",
        "\u62f7\u8d1d",
        "\u62ff\u8fc7\u6765",
        "\u5e26\u8fc7\u6765",
        "\u8f6c\u6210\u4eca\u5929",
        "\u8f6c\u5230\u4eca\u5929",
        "\u5230\u4eca\u5929",
        "\u548c\u6628\u5929\u4e00\u6837",
        "\u548c\u524d\u5929\u4e00\u6837",
        "\u8ba1\u5212\u90fd\u5b8c\u6210",
        "\u5168\u90e8\u5b8c\u6210",
        "\u90fd\u5b8c\u6210",
    )
    return any(marker in text for marker in safe_markers)


def _has_historical_mutation_marker(text: str) -> bool:
    mutation_markers = (
        "\u5220",
        "\u5220\u9664",
        "\u5220\u6389",
        "\u6e05\u7a7a",
        "\u64a4\u56de",
        "\u64a4\u9500",
        "\u4fee\u6539",
        "\u66f4\u6539",
        "\u6539\u6210",
        "\u6539\u4e3a",
        "\u66ff\u6362",
        "\u79fb\u5230",
        "\u79fb\u52a8",
        "\u5408\u5e76",
        "\u62c6\u5206",
    )
    if any(marker in text for marker in mutation_markers):
        return True
    return "\u6539" in text and ("\u65e5\u62a5" in text or "\u65e5\u5fd7" in text)


def _absolute_report_date(target_date: str) -> date | None:
    value = str(target_date or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _relative_date_offset(target_date: str) -> int:
    if target_date == "yesterday":
        return 1
    if target_date == "day_before_yesterday":
        return 2
    if target_date == "three_days_ago":
        return 3
    return 0


def _has_confirm_submit(commands: list[DailyCommand], status: str) -> bool:
    return status == "completed" and any(command.operation == "confirm" for command in commands)


async def _create_agent2_ledger_event(
    session: "AsyncSession",
    *,
    user: "User",
    report: "DailyReport",
    report_date: date,
    message_text: str,
    commands: list[DailyCommand],
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    actions: list[dict[str, Any]],
) -> None:
    from app.models import ReportInteractionEvent

    event = ReportInteractionEvent(
        user_id=user.id,
        report_id=report.id,
        dingtalk_user_id=getattr(user, "dingtalk_user_id", None),
        report_date=report_date,
        message_text=message_text,
        llm_decision_json={
            "agent2": True,
            "commands": [command.as_dict() for command in commands],
            "actions": actions,
        },
        backend_action="agent2_daily_execute",
        before_snapshot_json=before_snapshot,
        after_snapshot_json=after_snapshot,
        correction_type="",
        correction_from="",
        correction_to="",
        confidence=Decimal("1.0000"),
        is_undo=False,
        is_repeated_item_edit=False,
        asr_suspect_json={},
    )
    session.add(event)
    await session.flush()


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _merge_ordered(existing: list[str], incoming: list[str]) -> list[str]:
    result = list(existing or [])
    seen = {item.strip() for item in result if item.strip()}
    for item in incoming:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _now_in_timezone(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name or "Asia/Shanghai"))


def _clean_items(values: list[str]) -> list[str]:
    return [str(value).strip() for value in values if str(value or "").strip()]


def _completed_previous_plan_items(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = _completion_text_from_previous_plan_item(str(value or ""))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _completion_text_from_previous_plan_item(value: str) -> str:
    text = _clean_edit_value(value)
    if not text:
        return ""
    text = re.sub(r"^(?:明天|明日|明儿|明个|次日|后续|今天|今日)\s*", "", text)
    text = re.sub(r"^(?:计划|安排|待办|事项)\s*", "", text)
    text = re.sub(r"^(?:继续|持续|开始做|开始|做)\s*", "", text)
    text = _clean_edit_value(text)
    if not text:
        return ""
    if text.startswith("完成") or text.startswith("已完成"):
        return text
    return f"完成{text}"


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000:：,，.。;；!！?？()（）\[\]【】\"'“”‘’]+", "", str(value or "")).lower()


def _attach_agent2_item_ids(
    section_status: dict[str, Any],
    *,
    existing: "DailyReport | None",
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    item_ids: dict[str, list[str]] | None = None,
) -> None:
    if item_ids is not None:
        section_status[DRAFT_ITEM_IDS_KEY] = _normalize_item_ids_for_values(
            item_ids,
            {"today_work": today_work, "problems": problems, "tomorrow_plan": tomorrow_plan},
        )
        return
    existing_ids = _draft_item_ids(existing)
    existing_values = {
        "today_work": list(getattr(existing, "today_work", []) or []) if existing else [],
        "problems": list(getattr(existing, "problems", []) or []) if existing else [],
        "tomorrow_plan": list(getattr(existing, "tomorrow_plan", []) or []) if existing else [],
    }
    old_pool: dict[str, list[str]] = {}
    for field, values in existing_values.items():
        ids = existing_ids.get(field, [])
        for index, value in enumerate(values):
            if index < len(ids):
                old_pool.setdefault(value, []).append(ids[index])

    section_status[DRAFT_ITEM_IDS_KEY] = {
        "today_work": _resolve_item_ids("today_work", today_work, existing_values.get("today_work", []), existing_ids.get("today_work", []), old_pool),
        "problems": _resolve_item_ids("problems", problems, existing_values.get("problems", []), existing_ids.get("problems", []), old_pool),
        "tomorrow_plan": _resolve_item_ids("tomorrow_plan", tomorrow_plan, existing_values.get("tomorrow_plan", []), existing_ids.get("tomorrow_plan", []), old_pool),
    }


def _attach_agent2_edit_memory(section_status: dict[str, Any], application: DailyCommandApplication) -> None:
    if application.changed:
        clear_pending_daily_candidate(section_status)
    last_modified = _last_modified_item_from_application(application)
    if last_modified:
        section_status["_agent2_last_modified_item"] = last_modified
        section_status["_last_modified_item"] = last_modified
        section_status["_correction_target"] = last_modified
    elif _has_changed_destructive_action(application.actions):
        for key in ("_agent2_last_modified_item", "_last_modified_item", "_correction_target"):
            section_status.pop(key, None)
    last_deleted = _last_deleted_item_from_actions(application.actions)
    if last_deleted:
        section_status["_agent2_last_deleted_item"] = last_deleted


def _has_changed_destructive_action(actions: list[dict[str, Any]]) -> bool:
    for action in actions:
        if not action.get("changed"):
            continue
        if action.get("edit_action") == "delete_item" or action.get("operation") in {"clear", "copy_previous"}:
            return True
    return False


def _last_modified_item_from_application(application: DailyCommandApplication) -> dict[str, Any] | None:
    field_values = {
        "today_work": application.today_work,
        "problems": application.problems,
        "tomorrow_plan": application.tomorrow_plan,
    }
    item_ids = application.item_ids
    for action in reversed(application.actions):
        if not action.get("changed"):
            continue
        if action.get("edit_action") == "delete_item" or action.get("operation") in {"clear", "revoke", "confirm"}:
            continue
        field = str(action.get("target_field") or "")
        if field not in REPORT_FIELDS:
            continue
        index = _target_index_from_action(action, item_ids.get(field, []))
        if index < 1 or index > len(field_values.get(field, [])):
            continue
        item_id = _item_id_at(item_ids, field, index)
        return {
            "field": field,
            "section": field,
            "item_index": index,
            "item_id": item_id,
            "text": field_values[field][index - 1],
            "source": "agent2",
        }
    return None


def _target_index_from_action(action: dict[str, Any], current_ids: list[str]) -> int:
    target_indices = action.get("target_item_indices")
    if isinstance(target_indices, list):
        cleaned = [int(value) for value in target_indices if _is_positive_int(value)]
        if cleaned:
            return cleaned[-1]
    item_ids = [str(value) for value in action.get("item_ids", []) if str(value).strip()] if isinstance(action.get("item_ids"), list) else []
    for item_id in reversed(item_ids):
        if item_id in current_ids:
            return current_ids.index(item_id) + 1
    item_indices = action.get("item_indices")
    if isinstance(item_indices, list):
        cleaned = [int(value) for value in item_indices if _is_positive_int(value)]
        if cleaned:
            return min(cleaned)
    if action.get("edit_action") == "replace_field":
        return 1
    return 0


def _last_deleted_item_from_actions(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    for action in reversed(actions):
        if action.get("edit_action") != "delete_item" or not action.get("changed"):
            continue
        field = str(action.get("target_field") or "")
        if field not in REPORT_FIELDS:
            continue
        return {
            "field": field,
            "section": field,
            "item_indices": list(action.get("item_indices") or []),
            "item_ids": list(action.get("removed_item_ids") or action.get("item_ids") or []),
            "source": "agent2",
        }
    return None


def _is_positive_int(value: Any) -> bool:
    try:
        return int(value) >= 1
    except (TypeError, ValueError):
        return False


def _draft_item_ids(existing: "DailyReport | None") -> dict[str, list[str]]:
    if existing is None:
        return {}
    return _draft_item_ids_from_status(getattr(existing, "section_status", None))


def _draft_item_ids_from_status(section_status: dict[str, Any] | None) -> dict[str, list[str]]:
    if not isinstance(section_status, dict):
        return {}
    raw = section_status.get(DRAFT_ITEM_IDS_KEY)
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for field in REPORT_FIELD_ORDER:
        values = raw.get(field)
        if isinstance(values, list):
            result[field] = [str(value) for value in values if str(value).strip()]
    return result


def _resolve_item_ids(
    field: str,
    values: list[str],
    previous_values: list[str],
    previous_ids: list[str],
    old_pool: dict[str, list[str]],
) -> list[str]:
    result: list[str] = []
    used: set[str] = set()
    for index, value in enumerate(values):
        item_id = ""
        if index < len(previous_ids) and index < len(previous_values) and previous_values[index] == value:
            item_id = previous_ids[index]
        if not item_id:
            for candidate in old_pool.get(value, []):
                if candidate not in used:
                    item_id = candidate
                    break
        if not item_id:
            item_id = _make_item_id(field, index + 1, value)
        result.append(item_id)
        used.add(item_id)
    return result


def _make_item_id(field: str, index: int, value: str) -> str:
    digest = hashlib.sha1(f"{field}:{index}:{value}".encode("utf-8")).hexdigest()[:12]
    return f"di_{digest}"


def _completeness(today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> float:
    filled = sum(1 for values in (today_work, problems, tomorrow_plan) if values)
    return round(filled / 3, 4)


def _read_only_message(report: DailyReport | None, report_date: date, actions: list[dict[str, Any]] | None = None) -> str:
    action_operations = {str(action.get("operation") or "") for action in list(actions or [])}
    if "begin_edit" in action_operations:
        if report is None:
            return (
                f"\u53ef\u4ee5\uff0c{report_date.isoformat()} \u7684\u65e5\u62a5\u6211\u80fd\u5e2e\u4f60\u6539\u3002\n"
                "\u4e0d\u8fc7\u6211\u8fd8\u6ca1\u67e5\u5230\u8fd9\u5929\u7684\u8349\u7a3f\u3002\u4f60\u53ef\u4ee5\u76f4\u63a5\u8bf4\u8981\u8865\u54ea\u4e00\u680f\uff0c\u6216\u53d1\u5b8c\u6574\u65e5\u62a5\u5185\u5bb9\u3002"
            )
        return (
            f"\u53ef\u4ee5\uff0c\u6211\u73b0\u5728\u6309 {report_date.isoformat()} \u7684\u65e5\u62a5\u7ed9\u4f60\u6539\u3002\n"
            "\u4f60\u76f4\u63a5\u8bf4\u8981\u6539\u54ea\u4e00\u680f\u6216\u54ea\u4e00\u6761\u5c31\u884c\uff0c\u6bd4\u5982\uff1a\n"
            "\u4eca\u65e5\u5de5\u4f5c\u7b2c4\u6761\u5220\u6389\n"
            "\u95ee\u9898/\u98ce\u9669\u6539\u6210\u6682\u65e0\n"
            "\u660e\u65e5\u8ba1\u5212\u8865\u4e00\u6761\u2026\u2026\n\n"
            "\u5f53\u524d\u8fd9\u4efd\u662f\uff1a\n\n"
            + _format_report_preview(report_date, list(report.today_work or []), list(report.problems or []), list(report.tomorrow_plan or []))
        )
    if report is None:
        return f"我没查到 {report_date.isoformat()} 的日报记录。"
    return (
        f"我帮你查到 {report_date.isoformat()} 的日报：\n\n"
        + _format_report_preview(report_date, list(report.today_work or []), list(report.problems or []), list(report.tomorrow_plan or []))
    )


def _no_change_message(actions: list[dict[str, Any]]) -> str:
    reasons = {str(action.get("reason") or "") for action in actions}
    safety_flags = {
        str(flag)
        for action in actions
        for flag in list(action.get("safety_flags") or [])
    }
    if "historical_daily_mutation_blocked_after_cutoff" in safety_flags:
        return (
            "我没改历史日报。9点后昨天及更早日报只支持查看，或把内容复制到今天，不能再直接编辑、清空或撤回。\n"
            "你可以说“查看昨日日报”“复制昨天日报”，或者“今天和昨天工作一样”。"
        )
    if any(action.get("reason") == "previous_report_missing" for action in actions):
        return "没有找到可复制的上一份日报。"
    copy_actions = [action for action in actions if action.get("operation") == "copy_previous"]
    if copy_actions:
        action = copy_actions[0]
        target = str(action.get("target_field") or "all")
        copied_item_ids = list(action.get("item_ids") or [])
        if target == "today_work" and not copied_item_ids:
            return "昨天日报的【今日工作】是空的，或已经都在当前草稿里了，所以这次没有新增。若你想把昨天的【明日计划】转成今天已完成事项，可以说“昨天的计划都完成了”。"
        if target in REPORT_FIELDS and not copied_item_ids:
            return f"昨天日报的【{_field_label(target)}】没有可新增内容，所以这次没有变更。"
        if target == "all" and not copied_item_ids:
            return "昨天日报没有可复制的内容，所以这次没有变更。"
        return "昨天日报的内容已经在当前草稿里了，所以没有重复添加。"
    if any(action.get("reason") in {"previous_plan_missing", "previous_plan_empty"} for action in actions):
        return "没有找到昨天的待办内容。"
    if any(action.get("reason") == "report_not_completed" for action in actions):
        return "当前日报还不是已提交状态，不需要撤回。"
    if any(action.get("reason") == "incomplete_report" for action in actions):
        return "当前日报还没填完整，暂不能提交。"
    if any(action.get("reason") == "completed_report_locked" for action in actions):
        return "当前日报已提交，请先撤回后再修改。"
    if "ambiguous_target" in reasons:
        return "我没有改动日报。请明确要操作哪一条，例如回复“删除第 2 条”或“合并第 1、2 条”。"
    if "target_not_found" in reasons:
        return "我没有找到你指定的条目，日报未改动。请先查看当前草稿并重新指定栏目和编号。"
    if "version_conflict" in reasons:
        return "日报刚刚已被其他操作更新，这次没有覆盖新内容。请查看最新草稿后再操作。"
    if "duplicate_message" in reasons:
        return "这条请求已经处理过了，没有重复写入日报。"
    if "forbidden_payload" in reasons:
        return "这段内容更像操作指令、问答或闲聊，我没有把它写进日报正文。"
    if reasons & {"merge_indices_unresolved", "delete_indices_unresolved", "replace_indices_unresolved"}:
        return "没有找到对应编号，请先查看当前日报草稿后再修改。"
    if "replace_text_unresolved" in reasons:
        return "没有在当前日报草稿里找到要替换的内容，请换一种更明确的说法。"
    if "move_target_unresolved" in reasons:
        return "没有定位到要移动的条目或目标栏目，请说明要把哪一条移到哪一栏。"
    if "compound_edit_unresolved" in reasons:
        return "这句话里同时包含多个编辑动作，我没有定位清楚，请拆成一句一个动作。"
    if "unsupported_edit" in reasons:
        return "我理解你想修改日报，但还没有定位清楚要改哪一条。请带上栏目或编号再说一次。"
    return (
        "\u6211\u8fd8\u6ca1\u6539\u52a8\u65e5\u62a5\u3002"
        "\u4f60\u53ef\u4ee5\u76f4\u63a5\u8bf4\u5177\u4f53\u8981\u6539\u54ea\u4e00\u680f\u6216\u54ea\u4e00\u6761\uff0c"
        "\u6bd4\u5982\u201c\u4eca\u65e5\u5de5\u4f5c\u7b2c4\u6761\u5220\u6389\u201d\u6216\u201c\u95ee\u9898/\u98ce\u9669\u6539\u6210\u6682\u65e0\u201d\u3002"
    )


def _write_message(application: DailyCommandApplication, report_date: date) -> str:
    clear_actions = [action for action in application.actions if action.get("operation") == "clear"]
    if clear_actions:
        target = str(clear_actions[-1].get("target_field") or "all")
        if target == "all":
            prefix = f"已清空 {report_date.isoformat()} 的日报草稿，当前内容如下："
        elif target in REPORT_FIELDS:
            prefix = f"已清空 {report_date.isoformat()} 日报里的【{_field_label(target)}】，当前内容如下："
        else:
            prefix = f"已更新 {report_date.isoformat()} 日报，当前内容如下："
        return prefix + "\n\n" + _format_report_preview(report_date, application.today_work, application.problems, application.tomorrow_plan)
    return (
        f"已记录到 {report_date.isoformat()} 日报，当前草稿如下：\n\n"
        + _format_report_preview(report_date, application.today_work, application.problems, application.tomorrow_plan)
    )


def _format_report_preview(report_date: date, today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> str:
    return "\n".join(
        [
            f"当前填报日期：{report_date.isoformat()}",
            "",
            "当前日报草稿：",
            "",
            "今日工作：",
            *_numbered_or_empty(today_work),
            "",
            "问题/风险：",
            *_numbered_or_empty(problems),
            "",
            "明日计划：",
            *_numbered_or_empty(tomorrow_plan),
        ]
    )


def _numbered_or_empty(values: list[str]) -> list[str]:
    if not values:
        return ["暂无"]
    return [f"{index}. {value}" for index, value in enumerate(values, start=1)]


def _field_label(field: str) -> str:
    return {
        "today_work": "今日工作",
        "problems": "问题/风险",
        "tomorrow_plan": "明日计划",
    }.get(field, field)
