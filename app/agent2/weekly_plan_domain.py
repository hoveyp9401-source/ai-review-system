from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from app.agent2.weekly_plan_models import (
    WeeklyPlan,
    WeeklyPlanAuditEvent,
    WeeklyPlanBatch,
    WeeklyPlanCommand,
    WeeklyPlanDay,
    WeeklyPlanExecution,
    WeeklyPlanItem,
    WeeklyPlanReceipt,
    WeeklyPlanRosterMember,
)
from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    TrustedSourceKind,
    TrustedSuggestionEvidence,
    WeeklyPlanSuggestion,
    accept_suggestion,
    build_trusted_evidence,
    create_suggestion,
    reject_suggestion,
)


def weekly_plan_submission_timing(
    plan: WeeklyPlan,
    *,
    deadline_at: datetime,
    late_fill_until: datetime | None = None,
) -> str:
    """Derive timeliness without adding a second mutable plan status."""

    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise ValueError("deadline_at must be timezone-aware")
    if plan.submitted_at is None:
        return "not_submitted"
    if plan.submitted_at.tzinfo is None or plan.submitted_at.utcoffset() is None:
        raise ValueError("submitted_at must be timezone-aware")
    if late_fill_until is not None:
        if late_fill_until.tzinfo is None or late_fill_until.utcoffset() is None:
            raise ValueError("late_fill_until must be timezone-aware")
        if plan.submitted_at > late_fill_until:
            return "closed_window"
    return "late" if plan.submitted_at > deadline_at else "on_time"


def _stable_id(kind: str, *parts: object) -> str:
    return str(uuid5(NAMESPACE_URL, ":".join((kind, *(str(part) for part in parts)))))


def create_weekly_plan_batch(
    *,
    tenant_id: str,
    target_week_start: date,
    roster: tuple[WeeklyPlanRosterMember, ...],
    created_at: datetime,
) -> WeeklyPlanBatch:
    if target_week_start.weekday() != 0:
        raise ValueError("target_week_start must be a Monday")
    if not tenant_id.strip():
        raise ValueError("tenant_id is required")
    user_ids = [member.user_id for member in roster]
    if not all(user_ids) or len(user_ids) != len(set(user_ids)):
        raise ValueError("roster user ids must be non-empty and unique")
    return WeeklyPlanBatch(
        batch_id=_stable_id("weekly-plan-batch", tenant_id, target_week_start.isoformat()),
        tenant_id=tenant_id,
        target_week_start=target_week_start,
        roster=tuple(roster),
        created_at=created_at,
    )


def create_weekly_plan(
    *,
    batch: WeeklyPlanBatch,
    owner_user_id: str,
    created_at: datetime,
) -> WeeklyPlan:
    if owner_user_id not in {member.user_id for member in batch.roster}:
        raise ValueError("owner is not in the batch roster snapshot")
    plan_id = _stable_id(
        "weekly-plan", batch.tenant_id, owner_user_id, batch.target_week_start.isoformat()
    )
    days = tuple(
        WeeklyPlanDay(
            day_id=_stable_id("weekly-plan-day", plan_id, offset),
            plan_date=batch.target_week_start + timedelta(days=offset),
        )
        for offset in range(6)
    )
    return WeeklyPlan(
        plan_id=plan_id,
        batch_id=batch.batch_id,
        tenant_id=batch.tenant_id,
        owner_user_id=owner_user_id,
        target_week_start=batch.target_week_start,
        status="collecting",
        version=0,
        days=days,
        created_at=created_at,
        updated_at=created_at,
    )


def add_weekly_plan_suggestion(
    *,
    plan: WeeklyPlan,
    evidence: TrustedSuggestionEvidence,
    matter_excerpt: str,
    created_at: datetime,
    expires_at: datetime,
) -> WeeklyPlan:
    suggestion = create_suggestion(
        owner_user_id=plan.owner_user_id,
        target_week_start=plan.target_week_start,
        evidence=evidence,
        matter_excerpt=matter_excerpt,
        created_at=created_at,
        expires_at=expires_at,
    )
    if any(
        item.suggestion_id == suggestion.suggestion_id for item in plan.suggestions
    ):
        return plan
    return replace(
        plan,
        suggestions=(*plan.suggestions, suggestion),
        updated_at=created_at,
    )


SUPPORTED_COMMANDS = frozenset(
    {
        "add_item",
        "edit_item",
        "move_item",
        "delete_item",
        "set_day_empty",
        "preview_plan",
        "submit_plan",
        "accept_suggestion",
        "reject_suggestion",
        "capture_suggestion",
    }
)


def execute_weekly_plan_command(
    command: WeeklyPlanCommand,
    *,
    plan: WeeklyPlan,
    executed_at: datetime,
    executed_idempotency_keys: frozenset[str] = frozenset(),
    allow_submitted_intermediate_unresolved: bool = False,
) -> WeeklyPlanExecution:
    reason = _validate_command(
        command,
        plan,
        executed_idempotency_keys,
        executed_at=executed_at,
        allow_submitted_intermediate_unresolved=allow_submitted_intermediate_unresolved,
    )
    if reason:
        return _execution(command, plan, plan, executed_at, "duplicate" if reason == "duplicate" else "blocked", reason)
    if command.command_type == "preview_plan":
        return _execution(command, plan, plan, executed_at, "executed", "read_only")

    after = _apply_command(command, plan, executed_at)
    if (
        plan.status == "submitted"
        and not allow_submitted_intermediate_unresolved
        and any(day.state == "unfilled" for day in after.days)
    ):
        return _execution(
            command,
            plan,
            plan,
            executed_at,
            "blocked",
            "submitted_revision_unresolved_days",
        )
    return _execution(command, plan, after, executed_at, "executed", "ok")


def execute_weekly_plan_batch(
    commands: tuple[WeeklyPlanCommand, ...],
    *,
    plan: WeeklyPlan,
    executed_at: datetime,
    executed_idempotency_keys: frozenset[str] = frozenset(),
) -> WeeklyPlanExecution:
    """Validate all operations against a copy and expose only one final change."""

    if not commands:
        raise ValueError("weekly_plan_command_batch_required")
    first = commands[0]
    request_hash = _command_batch_sha256(commands)
    first = replace(first, patch={**first.patch, "_batch_sha256": request_hash})
    if first.idempotency_key in executed_idempotency_keys:
        return _execution(first, plan, plan, executed_at, "duplicate", "duplicate")
    if any(
        command.tenant_id != first.tenant_id
        or command.actor_user_id != first.actor_user_id
        or command.plan_id != first.plan_id
        for command in commands
    ):
        return _execution(
            first, plan, plan, executed_at, "blocked", "batch_scope_mismatch"
        )
    if first.expected_version != plan.version:
        return _execution(
            first, plan, plan, executed_at, "blocked", "version_conflict"
        )
    if len({command.command_id for command in commands}) != len(commands):
        return _execution(
            first, plan, plan, executed_at, "blocked", "duplicate_command_id"
        )
    current = plan
    for command in commands:
        candidate = replace(command, expected_version=current.version)
        execution = execute_weekly_plan_command(
            candidate,
            plan=current,
            executed_at=executed_at,
            allow_submitted_intermediate_unresolved=(plan.status == "submitted"),
        )
        if execution.receipt.status != "executed":
            return _execution(
                first,
                plan,
                plan,
                executed_at,
                "blocked",
                execution.receipt.reason_code,
            )
        current = execution.after
        if plan.status == "submitted":
            current = replace(
                current,
                status="submitted",
                submitted_at=plan.submitted_at,
            )
    if plan.status == "submitted" and any(
        day.state == "unfilled" for day in current.days
    ):
        return _execution(
            first,
            plan,
            plan,
            executed_at,
            "blocked",
            "submitted_revision_unresolved_days",
        )
    final_status = current.status
    if plan.status == "submitted":
        final_status = "submitted"
    current = replace(
        current,
        status=final_status,
        version=plan.version + 1,
        submitted_at=plan.submitted_at if plan.status == "submitted" else current.submitted_at,
        updated_at=executed_at,
    )
    return _execution(first, plan, current, executed_at, "executed", "ok")


def _validate_command(
    command: WeeklyPlanCommand,
    plan: WeeklyPlan,
    executed_keys: frozenset[str],
    *,
    executed_at: datetime,
    allow_submitted_intermediate_unresolved: bool = False,
) -> str:
    if command.command_type not in SUPPORTED_COMMANDS:
        return "unsupported_command_type"
    if not command.idempotency_key.strip():
        return "idempotency_key_required"
    if not command.command_id.strip():
        return "command_id_required"
    if not command.source_message_id.strip():
        return "source_message_id_required"
    if command.idempotency_key in executed_keys:
        return "duplicate"
    if command.tenant_id != plan.tenant_id:
        return "tenant_mismatch"
    if command.actor_user_id != plan.owner_user_id:
        return "owner_mismatch"
    if command.plan_id != plan.plan_id:
        return "plan_mismatch"
    if command.expected_version != plan.version:
        return "version_conflict"
    if plan.status == "cancelled" and command.command_type != "preview_plan":
        return "plan_cancelled"
    if command.command_type == "submit_plan" and any(
        day.state == "unfilled" for day in plan.days
    ):
        return "unresolved_days"
    if command.command_type == "add_item":
        if not _valid_plan_date(command.patch.get("plan_date"), plan):
            return "invalid_plan_date"
        if not str(command.patch.get("original_text") or "").strip():
            return "empty_original_text"
        if not str(command.patch.get("source") or "").strip():
            return "source_required"
    if command.command_type == "set_day_empty":
        if not _valid_plan_date(command.patch.get("plan_date"), plan):
            return "invalid_plan_date"
        target = _day_for(plan, str(command.patch["plan_date"]))
        if target.items:
            return "day_has_items"
    if command.command_type in {"edit_item", "move_item", "delete_item"}:
        item_id = str(command.patch.get("item_id") or "")
        if not item_id or _item_location(plan, item_id) is None:
            return "item_not_found"
        if command.command_type == "edit_item" and not str(
            command.patch.get("original_text") or ""
        ).strip():
            return "empty_original_text"
        if command.command_type == "move_item" and not _valid_plan_date(
            command.patch.get("plan_date"), plan
        ):
            return "invalid_plan_date"
        if (
            command.command_type in {"delete_item", "move_item"}
            and plan.status == "submitted"
            and not allow_submitted_intermediate_unresolved
        ):
            location = _item_location(plan, item_id)
            if location is not None:
                day_index, _ = location
                moving_to_other_day = (
                    command.command_type == "move_item"
                    and str(command.patch.get("plan_date") or "")
                    != plan.days[day_index].plan_date.isoformat()
                )
                if len(plan.days[day_index].items) == 1 and (
                    command.command_type == "delete_item" or moving_to_other_day
                ):
                    return "submitted_revision_unresolved_days"
    if command.command_type in {"accept_suggestion", "reject_suggestion"}:
        suggestion = _suggestion_for(plan, str(command.patch.get("suggestion_id") or ""))
        if suggestion is None:
            return "suggestion_not_found"
        if suggestion.status is not SuggestionStatus.AVAILABLE:
            return "suggestion_already_resolved"
        if command.command_type == "accept_suggestion" and not _valid_plan_date(
            command.patch.get("plan_date"), plan
        ):
            return "invalid_plan_date"
    if command.command_type == "capture_suggestion":
        evidence_text = str(command.patch.get("evidence_text") or "")
        matter_excerpt = str(command.patch.get("matter_excerpt") or "")
        source_version = str(command.patch.get("source_version") or "")
        if not evidence_text.strip():
            return "suggestion_evidence_required"
        if not matter_excerpt.strip() or matter_excerpt not in evidence_text:
            return "suggestion_matter_not_grounded"
        if not source_version.strip():
            return "suggestion_source_version_required"
        try:
            expires_at = datetime.fromisoformat(
                str(command.patch.get("expires_at") or "")
            )
        except ValueError:
            return "suggestion_expiry_invalid"
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            return "suggestion_expiry_timezone_required"
        if expires_at <= executed_at:
            return "suggestion_expired"
    return ""


def _apply_command(
    command: WeeklyPlanCommand, plan: WeeklyPlan, executed_at: datetime
) -> WeeklyPlan:
    days = list(plan.days)
    if command.command_type == "add_item":
        target_index = _day_index(plan, str(command.patch["plan_date"]))
        item = WeeklyPlanItem(
            item_id=_stable_id("weekly-plan-item", command.tenant_id, command.command_id),
            original_text=str(command.patch["original_text"]).strip(),
            source=str(command.patch["source"]).strip(),
            created_at=executed_at,
            updated_at=executed_at,
            source_ref=command.source_message_id,
        )
        day = days[target_index]
        days[target_index] = replace(day, state="planned", items=(*day.items, item))
    elif command.command_type == "edit_item":
        day_index, item_index = _item_location(plan, str(command.patch["item_id"]))  # type: ignore[misc]
        day = days[day_index]
        items = list(day.items)
        items[item_index] = replace(
            items[item_index],
            original_text=str(command.patch["original_text"]).strip(),
            updated_at=executed_at,
        )
        days[day_index] = replace(day, items=tuple(items))
    elif command.command_type in {"move_item", "delete_item"}:
        day_index, item_index = _item_location(plan, str(command.patch["item_id"]))  # type: ignore[misc]
        source_day = days[day_index]
        source_items = list(source_day.items)
        item = source_items.pop(item_index)
        days[day_index] = replace(
            source_day,
            state="planned" if source_items else "unfilled",
            items=tuple(source_items),
        )
        if command.command_type == "move_item":
            target_index = _day_index(plan, str(command.patch["plan_date"]))
            target_day = days[target_index]
            days[target_index] = replace(
                target_day,
                state="planned",
                items=(*target_day.items, replace(item, updated_at=executed_at)),
            )
    elif command.command_type == "set_day_empty":
        index = _day_index(plan, str(command.patch["plan_date"]))
        days[index] = replace(days[index], state="explicitly_empty")
    elif command.command_type == "submit_plan":
        return replace(
            plan,
            status="submitted",
            version=plan.version + 1,
            submitted_at=plan.submitted_at or executed_at,
            updated_at=executed_at,
        )
    elif command.command_type == "capture_suggestion":
        evidence = build_trusted_evidence(
            owner_user_id=plan.owner_user_id,
            source_kind=TrustedSourceKind.USER_ORIGINAL_MESSAGE,
            source_ref=command.source_message_id,
            source_version=str(command.patch["source_version"]),
            evidence_text=str(command.patch["evidence_text"]),
        )
        updated = add_weekly_plan_suggestion(
            plan=plan,
            evidence=evidence,
            matter_excerpt=str(command.patch["matter_excerpt"]),
            created_at=executed_at,
            expires_at=datetime.fromisoformat(str(command.patch["expires_at"])),
        )
        if updated is plan:
            return plan
        return replace(
            updated,
            version=plan.version + 1,
            updated_at=executed_at,
        )
    elif command.command_type in {"accept_suggestion", "reject_suggestion"}:
        suggestion_id = str(command.patch["suggestion_id"])
        suggestions = list(plan.suggestions)
        suggestion_index = next(
            index
            for index, suggestion in enumerate(suggestions)
            if suggestion.suggestion_id == suggestion_id
        )
        suggestion = suggestions[suggestion_index]
        if command.command_type == "accept_suggestion":
            target_index = _day_index(plan, str(command.patch["plan_date"]))
            target_day = days[target_index]
            item = WeeklyPlanItem(
                item_id=_stable_id(
                    "weekly-plan-item-from-suggestion", plan.plan_id, suggestion_id
                ),
                original_text=suggestion.matter_excerpt,
                source=f"accepted_suggestion:{suggestion.source_kind.value}",
                created_at=executed_at,
                updated_at=executed_at,
                source_ref=suggestion.suggestion_id,
            )
            days[target_index] = replace(
                target_day, state="planned", items=(*target_day.items, item)
            )
            suggestions[suggestion_index] = accept_suggestion(
                suggestion,
                decision_ref=command.source_message_id,
                decided_at=executed_at,
                accepted_item_id=item.item_id,
            )
        else:
            suggestions[suggestion_index] = reject_suggestion(
                suggestion,
                decision_ref=command.source_message_id,
                decided_at=executed_at,
            )
        resolved = all(day.state != "unfilled" for day in days)
        status = plan.status
        if plan.status in {"collecting", "pending_confirmation"}:
            status = "pending_confirmation" if resolved else "collecting"
        return replace(
            plan,
            days=tuple(days),
            suggestions=tuple(suggestions),
            status=status,
            version=plan.version + 1,
            updated_at=executed_at,
        )
    else:
        raise ValueError(f"command application not implemented: {command.command_type}")
    resolved = all(day.state != "unfilled" for day in days)
    status = plan.status
    if plan.status in {"collecting", "pending_confirmation"}:
        status = "pending_confirmation" if resolved else "collecting"
    return replace(
        plan,
        days=tuple(days),
        status=status,
        version=plan.version + 1,
        updated_at=executed_at,
    )


def _execution(
    command: WeeklyPlanCommand,
    before: WeeklyPlan,
    after: WeeklyPlan,
    executed_at: datetime,
    status: str,
    reason: str,
) -> WeeklyPlanExecution:
    actual_write = before != after
    receipt_id = _stable_id(
        "weekly-plan-receipt", command.tenant_id, command.idempotency_key
    )
    receipt = WeeklyPlanReceipt(
        receipt_id=receipt_id,
        tenant_id=command.tenant_id,
        idempotency_key=command.idempotency_key,
        command_id=command.command_id,
        command_type=command.command_type,
        actor_user_id=command.actor_user_id,
        source_message_id=command.source_message_id,
        request_sha256=_command_request_sha256(command),
        status=status,  # type: ignore[arg-type]
        reason_code=reason,
        plan_id=before.plan_id,
        actual_write=actual_write,
        before_version=before.version,
        after_version=after.version,
        created_at=executed_at,
    )
    audit = None
    if actual_write:
        audit = WeeklyPlanAuditEvent(
            audit_id=_stable_id("weekly-plan-audit", receipt_id),
            tenant_id=command.tenant_id,
            receipt_id=receipt_id,
            plan_id=before.plan_id,
            actor_user_id=command.actor_user_id,
            command_type=command.command_type,
            source_message_id=command.source_message_id,
            before=_plan_payload(before),
            after=_plan_payload(after),
            created_at=executed_at,
        )
    return WeeklyPlanExecution(command, before, after, receipt, audit)


def _plan_payload(plan: WeeklyPlan) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "version": plan.version,
        "status": plan.status,
        "days": [
            {
                "plan_date": day.plan_date.isoformat(),
                "state": day.state,
                "items": [
                    {
                        "item_id": item.item_id,
                        "original_text": item.original_text,
                        "source": item.source,
                    }
                    for item in day.items
                ],
            }
            for day in plan.days
        ],
        "suggestions": [
            {
                "suggestion_id": suggestion.suggestion_id,
                "matter_excerpt": suggestion.matter_excerpt,
                "status": suggestion.status.value,
                "source_kind": suggestion.source_kind.value,
                "source_ref": suggestion.source_ref,
                "source_version": suggestion.source_version,
                "evidence_sha256": suggestion.evidence_sha256,
                "decision_ref": suggestion.decision_ref,
                "accepted_item_id": suggestion.accepted_item_id,
                "superseded_by_id": suggestion.superseded_by_id,
            }
            for suggestion in plan.suggestions
        ],
    }


def _valid_plan_date(value: object, plan: WeeklyPlan) -> bool:
    try:
        candidate = date.fromisoformat(str(value))
    except ValueError:
        return False
    return candidate in {day.plan_date for day in plan.days}


def _day_index(plan: WeeklyPlan, value: str) -> int:
    candidate = date.fromisoformat(value)
    return next(index for index, day in enumerate(plan.days) if day.plan_date == candidate)


def _day_for(plan: WeeklyPlan, value: str) -> WeeklyPlanDay:
    return plan.days[_day_index(plan, value)]


def _item_location(plan: WeeklyPlan, item_id: str) -> tuple[int, int] | None:
    for day_index, day in enumerate(plan.days):
        for item_index, item in enumerate(day.items):
            if item.item_id == item_id:
                return day_index, item_index
    return None


def _suggestion_for(
    plan: WeeklyPlan, suggestion_id: str
) -> WeeklyPlanSuggestion | None:
    return next(
        (
            suggestion
            for suggestion in plan.suggestions
            if suggestion.suggestion_id == suggestion_id
        ),
        None,
    )


def _command_request_sha256(command: WeeklyPlanCommand) -> str:
    return sha256(
        json.dumps(
            {
                "command_id": command.command_id,
                "command_type": command.command_type,
                "tenant_id": command.tenant_id,
                "actor_user_id": command.actor_user_id,
                "plan_id": command.plan_id,
                "expected_version": command.expected_version,
                "source_message_id": command.source_message_id,
                "patch": command.patch,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _command_batch_sha256(commands: tuple[WeeklyPlanCommand, ...]) -> str:
    return sha256(
        json.dumps(
            [
                {
                    "command_id": command.command_id,
                    "command_type": command.command_type,
                    "patch": command.patch,
                    "source_message_id": command.source_message_id,
                }
                for command in commands
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
