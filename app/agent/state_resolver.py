from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any

from app.agent.action_plan import ActionPlan, AgentAction, PendingInteractionPlan
from app.agent.edit_cursor import (
    build_current_edit_cursor,
    build_historical_edit_cursor,
    cursor_focused_section,
    cursor_report,
    cursor_target_date,
    normalize_edit_cursor,
    with_edit_cursor,
)


REPORT_FIELDS = {"today_work", "problems", "tomorrow_plan"}


@dataclass(frozen=True)
class StateResolution:
    plan: ActionPlan
    branch: str


def resolve_pending_interaction(raw_input: str, pending_interaction: dict[str, Any] | None) -> StateResolution | None:
    if not isinstance(pending_interaction, dict):
        return None
    pending_type = str(pending_interaction.get("type") or "")
    operation = str(pending_interaction.get("operation") or "")
    target_field = str(pending_interaction.get("target_field") or "none")
    context = pending_interaction.get("context") if isinstance(pending_interaction.get("context"), dict) else {}
    edit_cursor = normalize_edit_cursor(pending_interaction)

    if pending_type == "pending_clarification":
        if operation == "clarification_only":
            resolved = _resolve_clarification_only(raw_input, target_field=target_field, context=context)
            if resolved is not None:
                return resolved
            reply = _confirmation_reply(raw_input)
            if reply == "cancel":
                return StateResolution(
                    plan=ActionPlan(
                        intent="edit_draft",
                        confidence="high",
                        should_write=False,
                        clear_pending_interaction=True,
                        reply_to_user="好的，先不处理这次调整，当前日报内容已保留。",
                        reason="Cancelled non-executable clarification.",
                    ),
                    branch="pending_clarification_only_cancel",
                )
            if reply == "confirm":
                confirmed = _confirmed_action_plan(operation="modify_report_item", target_field=target_field, context=context)
                if confirmed is not None and confirmed.should_write:
                    return StateResolution(plan=confirmed, branch="pending_clarification_only_confirm_action")
                return StateResolution(
                    plan=ActionPlan(
                        intent="edit_draft",
                        confidence="high",
                        should_write=False,
                        pending_interaction_to_set=PendingInteractionPlan(
                            type="pending_clarification",
                            operation=operation,
                            target_field=target_field if target_field in REPORT_FIELDS else "none",
                            context=context,
                        ),
                        reply_to_user="我刚才还缺具体信息，不能只靠“确认”执行。请直接说明要处理哪一栏、哪几条，比如“第5到第7条合并”。",
                        reason="Confirmation cannot execute a clarification-only pending state.",
                    ),
                    branch="pending_clarification_only_confirm",
                )
        clarification = _resolve_pending_clarification(raw_input, operation=operation, target_field=target_field, context=context)
        if clarification is not None:
            return clarification

    if pending_type == "awaiting_clarification" and operation == "split_or_append":
        split_resolution = _resolve_split_or_append_clarification(raw_input, target_field=target_field, context=context)
        if split_resolution is not None:
            return split_resolution

    if pending_type in {"awaiting_action_confirmation", "pending_batch_action"}:
        if operation not in {"unsubmit_report", "withdraw_and_modify"} and _looks_like_new_unsubmit_intent(raw_input):
            return _unsubmit_report_confirmation_plan(branch="switch_pending_to_unsubmit_report")

        target_completion = _pending_delete_target_completion(raw_input, operation=operation, target_field=target_field, context=context)
        if target_completion is not None:
            return target_completion

        reply = _confirmation_reply(raw_input)
        if reply == "confirm":
            plan = _confirmed_action_plan(operation=operation, target_field=target_field, context=context)
            if plan is not None:
                return StateResolution(plan=plan, branch=f"confirm_{operation or 'action'}")
        if reply == "cancel":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    clear_pending_interaction=True,
                    reply_to_user=_cancel_message(operation),
                    reason="Cancelled pending action confirmation.",
                ),
                branch=f"cancel_{operation or 'action'}",
            )

    if pending_type == "awaiting_append_target_confirmation" and target_field in {"today_work", "problems", "tomorrow_plan"}:
        _prefix, inline_content = _split_compound_confirmation(raw_input)
        if inline_content:
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    clear_pending_interaction=True,
                    actions=[AgentAction(type="append_items", field=target_field, items=[inline_content])],
                    reason="Confirmed pending append target with inline follow-up content.",
                ),
                branch="confirm_append_target_inline_content",
            )
        reply = _confirmation_reply(raw_input)
        candidate_reply = _quality_confirmation_reply(raw_input)
        candidate_items = _clean_candidate_items(context.get("candidate_items") or context.get("items") or context.get("candidate"))
        if candidate_items and (reply == "confirm" or candidate_reply == "accept"):
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    clear_pending_interaction=True,
                    actions=[AgentAction(type="append_items", field=target_field, items=candidate_items)],
                    reason="Confirmed pending append target with candidate content.",
                ),
                branch="confirm_append_target_candidate",
            )
        if reply == "confirm":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    reply_to_user=_append_content_message(target_field),
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="awaiting_append_content",
                        operation=operation or "append",
                        target_field=target_field,
                    ),
                    reason="Confirmed pending append target.",
                ),
                branch="confirm_append_target",
            )
        if reply == "cancel":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    reply_to_user="好的，请重新选择要补充的部分：今日工作、问题还是明日计划？",
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="awaiting_append_target",
                        operation=operation or "append",
                        target_field="none",
                    ),
                    reason="Cancelled pending append target confirmation.",
                ),
                branch="cancel_append_target",
            )

    if pending_type == "awaiting_append_target":
        target = _resolve_report_field_choice(raw_input)
        if target:
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    reply_to_user=_append_content_message(target),
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="awaiting_append_content",
                        operation=operation or "append",
                        target_field=target,
                    ),
                    reason="Resolved report field choice from pending append target state.",
                ),
                branch="select_append_target",
            )

    if pending_type == "awaiting_append_content" and target_field in {"today_work", "problems", "tomorrow_plan"}:
        append_reply = _append_content_reply(raw_input)
        if append_reply == "finish":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    clear_pending_interaction=True,
                    reply_to_user="\u597d\u7684\uff0c\u5148\u4e0d\u8865\u5145\u3002",
                    reason="Finished pending append content without adding more text.",
                ),
                branch="cancel_append_content",
            )
        if append_reply == "confirm":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    reply_to_user=_append_content_message(target_field),
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="awaiting_append_content",
                        operation=operation or "append",
                        target_field=target_field,
                    ),
                    reason="User confirmed readiness while content is still required.",
                ),
                branch="await_content_confirm_reprompt",
            )
        if append_reply == "cancel":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    clear_pending_interaction=True,
                    reply_to_user="\u597d\u7684\uff0c\u5df2\u53d6\u6d88\u672c\u6b21\u8865\u5145\u3002",
                    reason="Cancelled pending append content.",
                ),
                branch="cancel_append_content",
            )
        if _looks_like_plain_clarification_content(raw_input):
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    clear_pending_interaction=True,
                    actions=[
                        AgentAction(
                            type="append_items",
                            field=target_field,
                            items=[raw_input.strip()],
                            source="pending_append_content",
                        )
                    ],
                    reason="Resolved pending append content after the user had already selected a report field.",
                ),
                branch="pending_append_content",
            )

    if pending_type == "awaiting_content_quality_confirmation" and target_field in {"today_work", "problems", "tomorrow_plan"}:
        reply = _quality_confirmation_reply(raw_input)
        if reply == "accept":
            candidate_items = _clean_candidate_items(context.get("candidate_items") or context.get("items") or context.get("candidate"))
            if candidate_items:
                return StateResolution(
                    plan=ActionPlan(
                        intent="edit_draft",
                        confidence="high",
                        should_write=True,
                        clear_pending_interaction=True,
                        actions=[AgentAction(type="append_items", field=target_field, items=candidate_items)],
                        reason="Accepted original candidate content after quality clarification.",
                    ),
                    branch="accept_quality_candidate",
                )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    clear_pending_interaction=False,
                    reply_to_user=_append_content_message(target_field),
                    reason="Quality clarification candidate missing.",
                ),
                branch="quality_candidate_missing",
            )
        if reply == "cancel":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    clear_pending_interaction=True,
                    reply_to_user="好的，已取消本次补充。",
                    reason="Cancelled quality clarification candidate.",
                ),
                branch="cancel_quality_candidate",
            )

    if pending_type == "awaiting_dated_report_action":
        if _mentions_current_day_report(raw_input):
            return None
        target_date = cursor_target_date(edit_cursor) or str(context.get("target_date") or pending_interaction.get("target_date") or "")
        reply = _confirmation_reply(raw_input)
        if target_date and reply == "cancel":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    clear_pending_interaction=True,
                    reply_to_user=f"好的，先不修改 {target_date} 的日报。",
                    reason="Cancelled pending dated report action.",
                ),
                branch="cancel_pending_dated_report_action",
            )
        if target_date and reply == "confirm" and _dated_action_is_historical_edit(context, operation):
            next_context = _edit_flow_context(
                context,
                pending_type="historical_report_edit_flow",
                target_date=target_date,
                focused_section="none",
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    reply_to_user=_historical_edit_entry_message(target_date, next_context),
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="historical_report_edit_flow",
                        operation=operation or "modify_report",
                        target_field="none",
                        context=next_context,
                    ),
                    reason="Confirmed pending dated report edit action and entered historical edit flow.",
                ),
                branch="confirm_pending_dated_report_edit",
            )
        if target_date and _looks_like_display_request(raw_input):
            return StateResolution(
                plan=ActionPlan(
                    intent="query_history",
                    confidence="high",
                    should_write=False,
                    actions=[AgentAction(type="query_history", target_date=target_date)],
                    reason="Resolved display request against pending dated report context.",
                ),
                branch="query_pending_dated_report",
            )
        selected_field = _resolve_report_field_choice(raw_input) if _looks_like_field_selection_only(raw_input) else None
        if target_date and selected_field:
            next_context = dict(context)
            next_context.update(
                {
                    "target_date": target_date,
                    "requested_action": context.get("requested_action") or operation or "modify_report",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": selected_field,
                    "edit_cursor": build_historical_edit_cursor(
                        target_date=target_date,
                        current_report=context.get("current_report") if isinstance(context.get("current_report"), dict) else cursor_report(edit_cursor),
                        focused_section=selected_field,
                    ),
                }
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    reply_to_user=_historical_field_focus_message(selected_field, next_context),
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="historical_report_edit_flow",
                        operation=operation or "modify_report",
                        target_field=selected_field,
                        context=next_context,
                    ),
                    reason="Resolved historical report field choice and kept dated edit flow.",
                ),
                branch="select_historical_edit_field",
            )

    if pending_type == "historical_report_edit_flow":
        compact_input = _compact(raw_input)
        if any(marker in compact_input for marker in ("今天的日报", "今日日报", "当前日报", "现在的日报", "当前草稿")):
            return None
        target_date = cursor_target_date(edit_cursor) or str(context.get("target_date") or pending_interaction.get("target_date") or "")
        focus_section = cursor_focused_section(edit_cursor)
        if focus_section not in REPORT_FIELDS:
            focus_section = str(context.get("focus_section") or target_field or "none")
        full_report = _parse_full_report_replacement(raw_input)
        if target_date and full_report is not None:
            return _historical_full_report_replacement(
                full_report,
                target_date=target_date,
                focus_section=focus_section,
                context=context,
            )
        if _mentions_current_day_report(raw_input):
            return None
        if target_date and _looks_like_display_request(raw_input):
            return StateResolution(
                plan=ActionPlan(
                    intent="query_history",
                    confidence="high",
                    should_write=False,
                    actions=[AgentAction(type="query_history", field=focus_section, target_date=target_date)],
                    reason="Resolved display request against historical report edit flow.",
                ),
                branch="query_historical_edit_field",
            )
        reply = _confirmation_reply(raw_input)
        if reply == "cancel":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    clear_pending_interaction=True,
                    reply_to_user="好的，已取消修改历史日报。",
                    reason="Cancelled historical report edit flow.",
                ),
                branch="cancel_historical_edit_flow",
            )
        selected_field = _resolve_report_field_choice(raw_input) if _looks_like_field_selection_only(raw_input) else None
        if target_date and selected_field:
            next_context = dict(context)
            next_context.update(
                {
                    "target_date": target_date,
                    "requested_action": context.get("requested_action") or operation or "modify_report",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": selected_field,
                    "edit_cursor": build_historical_edit_cursor(
                        target_date=target_date,
                        current_report=context.get("current_report") if isinstance(context.get("current_report"), dict) else cursor_report(edit_cursor),
                        focused_section=selected_field,
                    ),
                }
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    reply_to_user=_historical_field_focus_message(selected_field, next_context, repeated=selected_field == focus_section),
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="historical_report_edit_flow",
                        operation=operation or "modify_report",
                        target_field=selected_field,
                        context=next_context,
                    ),
                    reason="Historical report edit field selected or repeated; keep focus and ask for edit content.",
                ),
                branch="historical_edit_field_focus_reprompt",
            )
        historical_instruction = _resolve_historical_edit_instruction(
            raw_input,
            target_date=target_date,
            focus_section=focus_section,
            context=context,
        )
        if historical_instruction is not None:
            return historical_instruction

    if pending_type == "current_report_edit_flow":
        if _mentions_previous_day_report(raw_input):
            return None
        target_date = cursor_target_date(edit_cursor) or str(context.get("target_date") or pending_interaction.get("target_date") or "")
        focus_section = cursor_focused_section(edit_cursor)
        if focus_section not in REPORT_FIELDS:
            focus_section = str(context.get("focus_section") or target_field or "none")
        if target_date and _looks_like_display_request(raw_input):
            return StateResolution(
                plan=ActionPlan(
                    intent="query_current",
                    confidence="high",
                    should_write=False,
                    reason="Resolved display request against current report edit flow.",
                ),
                branch="query_current_edit_flow",
            )
        if _looks_like_restore_recent_edit(raw_input):
            return None
        reply = _confirmation_reply(raw_input)
        if reply == "cancel":
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    clear_pending_interaction=True,
                    reply_to_user="好的，已退出修改今天日报。",
                    reason="Cancelled current report edit flow.",
                ),
                branch="cancel_current_edit_flow",
            )
        selected_field = _resolve_report_field_choice(raw_input) if _looks_like_field_selection_only(raw_input) else None
        if target_date and selected_field:
            next_context = dict(context)
            next_context.update(
                {
                    "target_date": target_date,
                    "requested_action": context.get("requested_action") or operation or "modify_report",
                    "stage": "awaiting_field_edit_content",
                    "focus_section": selected_field,
                    "edit_cursor": build_current_edit_cursor(
                        target_date=target_date,
                        current_report=context.get("current_report") if isinstance(context.get("current_report"), dict) else cursor_report(edit_cursor),
                        focused_section=selected_field,
                    ),
                }
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    reply_to_user=_current_field_focus_message(selected_field, next_context, repeated=selected_field == focus_section),
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="current_report_edit_flow",
                        operation=operation or "modify_report",
                        target_field=selected_field,
                        context=next_context,
                    ),
                    reason="Current report edit field selected or repeated; keep focus and ask for edit content.",
                ),
                branch="current_edit_field_focus_reprompt",
            )
        full_report = _parse_full_report_replacement(raw_input)
        if target_date and full_report is not None:
            return _current_full_report_replacement(
                full_report,
                target_date=target_date,
                focus_section=focus_section,
                context=context,
            )
        current_instruction = _resolve_current_edit_instruction(
            raw_input,
            target_date=target_date,
            focus_section=focus_section,
            context=context,
        )
        if current_instruction is not None:
            return current_instruction

    return None


def _resolve_pending_clarification(raw_input: str, *, operation: str, target_field: str, context: dict[str, Any]) -> StateResolution | None:
    if operation != "replace_text":
        return None
    old_value = str(context.get("candidate_old_text") or "").strip()
    new_value = str(context.get("candidate_new_text") or "").strip()
    if not old_value or not new_value:
        return None
    matches = context.get("matches") if isinstance(context.get("matches"), list) else []
    item_index = _extract_single_item_index(raw_input)
    if item_index is None and _confirmation_reply(raw_input) == "confirm":
        try:
            candidate_index = int(context.get("candidate_item_index") or 0)
        except (TypeError, ValueError):
            candidate_index = 0
        item_index = candidate_index or None
    selected: dict[str, Any] | None = None
    if item_index is not None:
        for match in matches:
            if not isinstance(match, dict):
                continue
            try:
                candidate_index = int(match.get("item_index") or 0)
            except (TypeError, ValueError):
                continue
            if candidate_index == item_index:
                selected = match
                break
        if selected is None and target_field in REPORT_FIELDS:
            selected = {"section": target_field, "item_index": item_index}
        if selected is None:
            report = _report_from_pending_context(context)
            resolved = _resolve_current_item_indices([item_index], field=target_field, report=report)
            if "error" not in resolved:
                field = str(resolved.get("field") or "none")
                indices = resolved.get("item_indices") if isinstance(resolved.get("item_indices"), list) else []
                if field in REPORT_FIELDS and indices:
                    resolved_index = int(indices[0])
                    values = report.get(field, [])
                    selected = {
                        "section": field,
                        "item_index": resolved_index,
                        "text": values[resolved_index - 1] if 1 <= resolved_index <= len(values) else "",
                    }
    if selected is None:
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                reply_to_user="你要修改哪一条？请直接说具体序号，比如“第7条”。",
                pending_interaction_to_set=PendingInteractionPlan(
                    type="pending_clarification",
                    operation=operation,
                    target_field=target_field if target_field in REPORT_FIELDS else "none",
                    context=context,
                ),
                reason="Pending clarification still lacks a target item index.",
            ),
            branch="pending_clarification_missing_item",
        )
    field = str(selected.get("section") or target_field or "none")
    try:
        index = int(selected.get("item_index") or 0)
    except (TypeError, ValueError):
        index = 0
    if field not in REPORT_FIELDS or index < 1:
        return None
    selected_text = str(selected.get("text") or "").strip()
    old_value_for_action = selected_text if selected_text and old_value not in selected_text else old_value
    return StateResolution(
        plan=ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            clear_pending_interaction=True,
            actions=[
                AgentAction(
                    type="replace_text",
                    field=field,
                    old_value=old_value_for_action,
                    new_value=new_value,
                    target_item_index=index,
                    source="pending_clarification_replace_text",
                )
            ],
            reason="Resolved pending clarification target item without confirmation.",
        ),
        branch="pending_clarification_replace_text",
    )


def _resolve_clarification_only(raw_input: str, *, target_field: str, context: dict[str, Any]) -> StateResolution | None:
    operation_intent = str(context.get("operation_intent") or "")
    if operation_intent == "merge_items":
        indices = _item_indices_from_text(raw_input)
        if not indices and _confirmation_reply(raw_input) == "confirm":
            indices = _clean_indices(context.get("candidate_indices"))
        if not indices:
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="pending_clarification",
                        operation="clarification_only",
                        target_field=target_field if target_field in REPORT_FIELDS else "none",
                        context=context,
                    ),
                    reply_to_user="你要合并哪几条？请直接说具体序号，比如“第5到第7条”。",
                    reason="Clarification-only merge still lacks item indices.",
                ),
                branch="pending_clarification_merge_missing_indices",
            )
        report = _report_from_pending_context(context)
        resolved = _resolve_current_item_indices(indices, field=target_field, report=report)
        if resolved.get("error"):
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="pending_clarification",
                        operation="clarification_only",
                        target_field=target_field if target_field in REPORT_FIELDS else "none",
                        context=context,
                    ),
                    reply_to_user=str(resolved["error"]),
                    reason="Clarification-only merge target indices could not be resolved.",
                ),
                branch="pending_clarification_merge_index_error",
            )
        field = str(resolved["field"])
        section_indices = list(resolved["item_indices"])
        if len(section_indices) < 2:
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=False,
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="pending_clarification",
                        operation="clarification_only",
                        target_field=target_field if target_field in REPORT_FIELDS else "none",
                        context=context,
                    ),
                    reply_to_user="合并至少需要两条内容，请说明要合并哪几条。",
                    reason="Clarification-only merge received fewer than two indices.",
                ),
                branch="pending_clarification_merge_too_few_indices",
            )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                clear_pending_interaction=True,
                actions=[AgentAction(type="merge_items", field=field, item_indices=section_indices, source="pending_clarification_merge")],
                reason="Resolved merge indices from pending clarification.",
            ),
            branch="pending_clarification_merge_items",
        )

    if operation_intent == "replace_text":
        next_context = dict(context)
        item_index = _extract_single_item_index(raw_input)
        if item_index is None and _confirmation_reply(raw_input) == "confirm":
            try:
                item_index = int(next_context.get("candidate_item_index") or 0)
            except (TypeError, ValueError):
                item_index = None
        if item_index is not None:
            next_context["matches"] = [
                {
                    "section": target_field if target_field in REPORT_FIELDS else str(next_context.get("candidate_field") or "today_work"),
                    "item_index": item_index,
                    "text": "",
                }
            ]
            return _resolve_pending_clarification(
                raw_input,
                operation="replace_text",
                target_field=target_field,
                context=next_context,
            )

    if target_field in REPORT_FIELDS and _looks_like_plain_clarification_content(raw_input):
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                clear_pending_interaction=True,
                actions=[
                    AgentAction(
                        type="append_items",
                        field=target_field,
                        items=[raw_input.strip()],
                        source="pending_clarification_append_content",
                    )
                ],
                reason="Resolved plain clarification content as an append to the selected report field.",
            ),
            branch="pending_clarification_append_content",
        )

    return None


def _resolve_split_or_append_clarification(raw_input: str, *, target_field: str, context: dict[str, Any]) -> StateResolution | None:
    if target_field not in REPORT_FIELDS:
        return None

    reply = _confirmation_reply(raw_input)
    if reply == "cancel":
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                clear_pending_interaction=True,
                reply_to_user="好的，先不拆分，当前内容已保留。",
                reason="Cancelled pending split-or-append clarification.",
            ),
            branch="pending_clarification_split_or_append_cancel",
        )

    wants_split = _looks_like_split_existing_item_request(raw_input)
    if reply != "confirm" and not wants_split:
        return None

    current_item_text = str(context.get("current_item_text") or "").strip()
    split_items = _clean_candidate_items(context.get("split_suggestion"))
    if len(split_items) < 2 and current_item_text and wants_split:
        split_items = _split_report_items(current_item_text, field=target_field)
    if len(split_items) < 2 or not current_item_text:
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                pending_interaction_to_set=PendingInteractionPlan(
                    type="awaiting_clarification",
                    operation="split_or_append",
                    target_field=target_field,
                    context=context,
                ),
                reply_to_user="我还缺拆分后的具体内容。请直接说要拆成哪几条，或回复“取消”。",
                reason="Pending split-or-append clarification lacks executable split items.",
            ),
            branch="pending_clarification_split_or_append_missing_items",
        )

    try:
        item_index = int(context.get("current_item_index") or 1)
    except (TypeError, ValueError):
        item_index = 1
    item_index = item_index if item_index > 0 else 1

    return StateResolution(
        plan=ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            clear_pending_interaction=True,
            actions=[
                AgentAction(
                    type="replace_text",
                    field=target_field,
                    old_value=current_item_text,
                    new_value="\n".join(split_items),
                    target_item_index=item_index,
                    source="pending_split_confirmation",
                )
            ],
            reason="Resolved pending split-or-append clarification into a split replacement.",
        ),
        branch="pending_clarification_split_or_append_confirm_split",
    )


def _looks_like_split_existing_item_request(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    if "新增" in compact or "添加" in compact or "补充" in compact:
        return False
    has_split_verb = "拆" in compact or "分" in compact
    has_split_shape = any(marker in compact for marker in ("两条", "二条", "两项", "二项", "分开", "拆开", "分别"))
    return has_split_verb and has_split_shape


def _looks_like_plain_clarification_content(raw_input: str) -> bool:
    text = str(raw_input or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if _confirmation_reply(text) is not None:
        return False
    field_labels = {
        "\u4eca\u65e5\u5de5\u4f5c",
        "\u4eca\u5929\u5de5\u4f5c",
        "\u5de5\u4f5c",
        "\u95ee\u9898",
        "\u98ce\u9669",
        "\u95ee\u9898\u98ce\u9669",
        "\u95ee\u9898/\u98ce\u9669",
        "\u660e\u65e5\u8ba1\u5212",
        "\u660e\u5929\u8ba1\u5212",
        "\u8ba1\u5212",
    }
    if compact in field_labels:
        return False
    if _looks_like_submit_request(text) or _looks_like_new_unsubmit_intent(text) or _looks_like_restore_recent_edit(text):
        return False
    if re.search(r"[?\uff1f]$", text):
        return False
    if re.search(r"(?:\u4ec0\u4e48|\u5565|\u54ea|\u600e\u4e48|\u5982\u4f55|\u662f\u5426|\u662f\u4e0d\u662f|\u80fd\u4e0d\u80fd|\u53ef\u4ee5\u5417|\u5417)$", compact):
        return False
    return True


def _extract_single_item_index(raw_input: str) -> int | None:
    text = _compact(raw_input)
    if not text:
        return None
    match = re.search(r"第?(\d{1,3}|[一二两三四五六七八九十]{1,3})(?:条|项|个)?", text)
    if not match:
        return None
    value = match.group(1)
    try:
        return int(value)
    except ValueError:
        return _parse_cn_number(value)


def _resolve_historical_edit_instruction(
    raw_input: str,
    *,
    target_date: str,
    focus_section: str,
    context: dict[str, Any],
) -> StateResolution | None:
    if not target_date:
        return None
    compact = _compact(raw_input)
    if not compact:
        return None
    report = _historical_report(context)
    explicit_field = _resolve_report_field_choice(raw_input)
    field = explicit_field if explicit_field in REPORT_FIELDS else (focus_section if focus_section in REPORT_FIELDS else "none")

    if _looks_like_delete_or_clear(compact):
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                clear_pending_interaction=True,
                reply_to_user="历史日报不能删除。我可以帮你查看该日报，或基于历史日报复制一份作为今日草稿。",
                reason="Blocked destructive edit against a historical report.",
            ),
            branch="historical_edit_delete_blocked",
        )

    append_value = _extract_append_value(raw_input)
    if append_value and field in REPORT_FIELDS:
        action = AgentAction(
            type="update_historical_report",
            field=field,
            items=[append_value],
            target_date=target_date,
            source="append_items",
            requires_confirmation=True,
            confirmation_message=f"确认向 {target_date} 日报的“{_field_label(field)}”补充“{append_value}”吗？",
        )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[action],
                reason="Resolved append inside historical report edit flow.",
            ),
            branch="historical_edit_append",
        )

    change = _parse_change_instruction(raw_input)
    if change is None:
        return None
    left, right = change
    item_indices = _item_indices_from_text(raw_input)
    if item_indices and right:
        resolved = _resolve_historical_item_indices(item_indices, field=field, report=report)
        if resolved.get("error"):
            return _historical_flow_reprompt(str(resolved["error"]), target_date=target_date, focus_section=focus_section, context=context)
        resolved_field = str(resolved["field"])
        action = AgentAction(
            type="update_historical_report",
            field=resolved_field,
            items=[right],
            item_indices=list(resolved["item_indices"]),
            target_date=target_date,
            source="replace_items",
            requires_confirmation=True,
            confirmation_message=_historical_replace_item_confirmation(target_date, resolved_field, list(resolved["item_indices"]), right, report),
        )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[action],
                reason="Resolved item replacement inside historical report edit flow.",
            ),
            branch="historical_edit_replace_item",
        )
    if field in REPORT_FIELDS and (_left_mentions_field(left, field) or (focus_section in REPORT_FIELDS and not left)):
        replacement_items = _split_replacement_items(right)
        action = AgentAction(
            type="update_historical_report",
            field=field,
            items=replacement_items,
            target_date=target_date,
            source="replace_field",
            requires_confirmation=True,
            confirmation_message=f"确认把 {target_date} 日报的“{_field_label(field)}”改成“{'；'.join(replacement_items)}”吗？",
        )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[action],
                reason="Resolved field replacement inside historical report edit flow.",
            ),
            branch="historical_edit_replace_field",
        )
    if left and right:
        matched_field = _field_containing_text(report, left)
        if matched_field:
            action = AgentAction(
                type="update_historical_report",
                field=matched_field,
                old_value=left,
                new_value=right,
                target_date=target_date,
                source="replace_text",
                requires_confirmation=True,
                confirmation_message=f"确认把 {target_date} 日报“{_field_label(matched_field)}”里的“{left}”改成“{right}”吗？",
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    actions=[action],
                    reason="Resolved text replacement inside historical report edit flow.",
                ),
                branch="historical_edit_replace_text",
            )
    return None


def _resolve_current_edit_instruction(
    raw_input: str,
    *,
    target_date: str,
    focus_section: str,
    context: dict[str, Any],
) -> StateResolution | None:
    if not target_date:
        return None
    compact = _compact(raw_input)
    if not compact:
        return None
    report = _historical_report(context)
    explicit_field = _resolve_report_field_choice(raw_input)
    field = explicit_field if explicit_field in REPORT_FIELDS else (focus_section if focus_section in REPORT_FIELDS else "none")

    batch_resolution = _resolve_current_batch_edit_instruction(
        raw_input,
        target_date=target_date,
        focus_section=field,
        context=context,
        report=report,
    )
    if batch_resolution is not None:
        return batch_resolution

    local_delete = _parse_local_text_delete_instruction(raw_input, report=report, field=field)
    if local_delete is not None:
        resolved_field, item_index, old_value, new_value = local_delete
        action = AgentAction(
            type="replace_text",
            field=resolved_field,
            old_value=old_value,
            new_value=new_value,
            target_item_index=item_index,
        )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[action],
                pending_interaction_to_set=PendingInteractionPlan(
                    type="current_report_edit_flow",
                    operation="modify_report",
                    target_field=resolved_field,
                    context=_edit_flow_context(
                        context,
                        pending_type="current_report_edit_flow",
                        target_date=target_date,
                        focused_section=resolved_field,
                    ),
                ),
                reason="Resolved local text delete inside current report edit flow.",
            ),
            branch="current_edit_local_text_delete",
        )

    if _looks_like_delete_or_clear(compact):
        item_indices = _item_indices_from_text(raw_input)
        if not item_indices:
            item_indices = _leading_numbered_item_indices(raw_input)
        if item_indices:
            resolved = _resolve_current_item_indices(item_indices, field=field, report=report)
            if resolved.get("error"):
                return _current_flow_reprompt(str(resolved["error"]), target_date=target_date, focus_section=focus_section, context=context)
            resolved_field = str(resolved["field"])
            section_indices = list(resolved["item_indices"])
            action = AgentAction(
                type="delete_item",
                field=resolved_field,
                item_indices=section_indices,
                requires_confirmation=len(section_indices) > 1,
                confirmation_message=_current_delete_confirmation(resolved_field, section_indices, report),
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    actions=[action],
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="current_report_edit_flow",
                        operation="modify_report",
                        target_field=resolved_field,
                        context=_edit_flow_context(
                            context,
                            pending_type="current_report_edit_flow",
                            target_date=target_date,
                            focused_section=resolved_field,
                        ),
                    ),
                    reason="Resolved item delete inside current report edit flow.",
                ),
                branch="current_edit_delete_item",
            )
        reference = _find_unique_item_reference(raw_input, report=report, field=field)
        if reference is not None:
            resolved_field, index, _item = reference
            action = AgentAction(
                type="delete_item",
                field=resolved_field,
                item_indices=[index],
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    actions=[action],
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="current_report_edit_flow",
                        operation="modify_report",
                        target_field=resolved_field,
                        context=_edit_flow_context(
                            context,
                            pending_type="current_report_edit_flow",
                            target_date=target_date,
                            focused_section=resolved_field,
                        ),
                    ),
                    reason="Resolved item delete by unique text reference inside current report edit flow.",
                ),
                branch="current_edit_delete_item_by_text",
            )
        if field in REPORT_FIELDS and (_looks_like_clear_field(compact) or explicit_field in REPORT_FIELDS or focus_section in REPORT_FIELDS):
            action = AgentAction(
                type="clear_field",
                field=field,
                requires_confirmation=True,
                confirmation_message=f"确认清空今天日报的“{_field_label(field)}”吗？",
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    actions=[action],
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="current_report_edit_flow",
                        operation="modify_report",
                        target_field=field,
                        context=_edit_flow_context(
                            context,
                            pending_type="current_report_edit_flow",
                            target_date=target_date,
                            focused_section=field,
                        ),
                    ),
                    reason="Resolved field clear inside current report edit flow.",
                ),
                branch="current_edit_clear_field",
            )
        return _current_flow_reprompt(
            "你想删除今天日报的哪一部分？可以说“今日工作第1条删掉”或“明日计划全删了”。",
            target_date=target_date,
            focus_section=focus_section,
            context=context,
        )

    append_value = _extract_append_value(raw_input)
    if append_value and field in REPORT_FIELDS:
        action = AgentAction(
            type="append_items",
            field=field,
            items=[append_value],
        )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[action],
                pending_interaction_to_set=PendingInteractionPlan(
                    type="current_report_edit_flow",
                    operation="modify_report",
                    target_field=field,
                    context=_edit_flow_context(
                        context,
                        pending_type="current_report_edit_flow",
                        target_date=target_date,
                        focused_section=field,
                    ),
                ),
                reason="Resolved append inside current report edit flow.",
            ),
            branch="current_edit_append",
        )

    spelled_correction = _parse_spelled_correction_instruction(raw_input, report=report, field=field)
    if spelled_correction is not None:
        resolved_field, item_index, old_value, new_value = spelled_correction
        action = AgentAction(
            type="replace_text",
            field=resolved_field,
            old_value=old_value,
            new_value=new_value,
            target_item_index=item_index,
        )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[action],
                pending_interaction_to_set=PendingInteractionPlan(
                    type="current_report_edit_flow",
                    operation="modify_report",
                    target_field=resolved_field,
                    context=_edit_flow_context(
                        context,
                        pending_type="current_report_edit_flow",
                        target_date=target_date,
                        focused_section=resolved_field,
                    ),
                ),
                reason="Resolved spelled correction inside current report edit flow.",
            ),
            branch="current_edit_spelled_correction",
        )

    change = _parse_change_instruction(raw_input)
    if change is None:
        return None
    left, right = change
    item_indices = _item_indices_from_text(raw_input)
    if item_indices and right:
        resolved = _resolve_current_item_indices(item_indices, field=field, report=report)
        if resolved.get("error"):
            return _current_flow_reprompt(str(resolved["error"]), target_date=target_date, focus_section=focus_section, context=context)
        resolved_field = str(resolved["field"])
        section_indices = list(resolved["item_indices"])
        if len(section_indices) != 1:
            return _current_flow_reprompt(
                "一次请只修改一条内容；可以说“今日工作第2条改成xxx”。",
                target_date=target_date,
                focus_section=resolved_field,
                context=context,
            )
        old_value = (report.get(resolved_field, []) or [])[section_indices[0] - 1]
        action = AgentAction(
            type="replace_text",
            field=resolved_field,
            old_value=old_value,
            new_value=right,
            target_item_index=section_indices[0],
        )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[action],
                pending_interaction_to_set=PendingInteractionPlan(
                    type="current_report_edit_flow",
                    operation="modify_report",
                    target_field=resolved_field,
                    context=_edit_flow_context(
                        context,
                        pending_type="current_report_edit_flow",
                        target_date=target_date,
                        focused_section=resolved_field,
                    ),
                ),
                reason="Resolved item replacement inside current report edit flow.",
            ),
            branch="current_edit_replace_item",
        )
    if field in REPORT_FIELDS and (_left_mentions_field(left, field) or (focus_section in REPORT_FIELDS and not left)):
        values = report.get(field, [])
        if len(values) == 1 and (not left or _left_mentions_field(left, field)):
            action = AgentAction(
                type="replace_text",
                field=field,
                old_value=values[0],
                new_value=right,
                target_item_index=1,
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    actions=[action],
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="current_report_edit_flow",
                        operation="modify_report",
                        target_field=field,
                        context=_edit_flow_context(
                            context,
                            pending_type="current_report_edit_flow",
                            target_date=target_date,
                            focused_section=field,
                        ),
                    ),
                    reason="Resolved focused single item replacement inside current report edit flow.",
                ),
                branch="current_edit_replace_focused_single_item",
            )
        replacement_items = _split_replacement_items(right)
        action = AgentAction(
            type="replace_field",
            field=field,
            items=replacement_items,
            requires_confirmation=True,
            confirmation_message=f"确认把今天日报的“{_field_label(field)}”改成“{'；'.join(replacement_items)}”吗？",
        )
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                actions=[action],
                pending_interaction_to_set=PendingInteractionPlan(
                    type="current_report_edit_flow",
                    operation="modify_report",
                    target_field=field,
                    context=_edit_flow_context(
                        context,
                        pending_type="current_report_edit_flow",
                        target_date=target_date,
                        focused_section=field,
                    ),
                ),
                reason="Resolved field replacement inside current report edit flow.",
            ),
            branch="current_edit_replace_field",
        )
    if left and right:
        matched_field = _field_containing_text(report, left)
        if matched_field:
            action = AgentAction(
                type="replace_text",
                field=matched_field,
                old_value=left,
                new_value=right,
            )
            return StateResolution(
                plan=ActionPlan(
                    intent="edit_draft",
                    confidence="high",
                    should_write=True,
                    actions=[action],
                    pending_interaction_to_set=PendingInteractionPlan(
                        type="current_report_edit_flow",
                        operation="modify_report",
                        target_field=matched_field,
                        context=_edit_flow_context(
                            context,
                            pending_type="current_report_edit_flow",
                            target_date=target_date,
                            focused_section=matched_field,
                        ),
                    ),
                    reason="Resolved text replacement inside current report edit flow.",
                ),
                branch="current_edit_replace_text",
            )
    return None


def _resolve_current_batch_edit_instruction(
    raw_input: str,
    *,
    target_date: str,
    focus_section: str,
    context: dict[str, Any],
    report: dict[str, list[str]],
) -> StateResolution | None:
    clauses = _split_action_clauses(raw_input)
    if len(clauses) < 2:
        return None

    actions: list[AgentAction] = []
    focused_section = focus_section if focus_section in REPORT_FIELDS else "none"
    saw_action_like_clause = False
    for clause in clauses:
        action_like = _looks_like_current_action_clause(clause)
        saw_action_like_clause = saw_action_like_clause or action_like
        action = _current_action_from_clause(clause, focus_section=focused_section, report=report)
        if action is None:
            if action_like:
                return None
            continue
        actions.append(action)
        if action.field in REPORT_FIELDS:
            focused_section = action.field
        elif action.target_field in REPORT_FIELDS:
            focused_section = action.target_field

    if len(actions) < 2 or not saw_action_like_clause:
        return None

    target_field = next((action.field for action in actions if action.field in REPORT_FIELDS), focused_section)
    if any(action.requires_confirmation for action in actions):
        pending_context = _edit_flow_context(
            context,
            pending_type="current_report_edit_flow",
            target_date=target_date,
            focused_section=target_field if target_field in REPORT_FIELDS else focused_section,
        )
        pending_context["actions"] = [action.model_dump() for action in actions]
        return StateResolution(
            plan=ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                reply_to_user=_current_batch_confirmation_message(actions, report),
                pending_interaction_to_set=PendingInteractionPlan(
                    type="pending_batch_action",
                    operation="batch_action",
                    target_field=target_field if target_field in REPORT_FIELDS else "none",
                    context=pending_context,
                ),
                reason="Resolved multiple current report edit actions that require confirmation.",
            ),
            branch="current_edit_batch_pending",
        )

    return StateResolution(
        plan=ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=actions,
            pending_interaction_to_set=PendingInteractionPlan(
                type="current_report_edit_flow",
                operation="modify_report",
                target_field=target_field if target_field in REPORT_FIELDS else focused_section,
                context=_edit_flow_context(
                    context,
                    pending_type="current_report_edit_flow",
                    target_date=target_date,
                    focused_section=target_field if target_field in REPORT_FIELDS else focused_section,
                ),
            ),
            reason="Resolved multiple current report edit actions.",
        ),
        branch="current_edit_batch",
    )


def _split_action_clauses(raw_input: str) -> list[str]:
    parts = re.split(r"(?:然后|接着|再|并且|同时|另外|[，,。；;]|\n+)", str(raw_input or ""))
    return [_strip_side(part) for part in parts if _strip_side(part)]


def _looks_like_current_action_clause(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    return any(
        marker in compact
        for marker in (
            "改成",
            "改为",
            "换成",
            "更正为",
            "修改为",
            "更新为",
            "删除",
            "删掉",
            "删了",
            "去掉",
            "移除",
            "清空",
            "补充",
            "添加",
            "增加",
            "加上",
            "补上",
            "撤回",
            "提交",
            "改一下",
            "括号",
            "就是",
        )
    )


def _current_action_from_clause(
    raw_input: str,
    *,
    focus_section: str,
    report: dict[str, list[str]],
) -> AgentAction | None:
    compact = _compact(raw_input)
    if not compact:
        return None
    if _looks_like_unsubmit_request(raw_input):
        return AgentAction(type="unsubmit_report", requires_confirmation=True, confirmation_message="确认撤回已提交日报吗？")
    if _looks_like_submit_request(raw_input):
        return AgentAction(type="submit_report")

    explicit_field = _resolve_report_field_choice(raw_input)
    field = explicit_field if explicit_field in REPORT_FIELDS else (focus_section if focus_section in REPORT_FIELDS else "none")
    local_delete = _parse_local_text_delete_instruction(raw_input, report=report, field=field)
    if local_delete is not None:
        resolved_field, item_index, old_value, new_value = local_delete
        return AgentAction(
            type="replace_text",
            field=resolved_field,
            old_value=old_value,
            new_value=new_value,
            target_item_index=item_index,
        )
    if _looks_like_delete_or_clear(compact):
        item_indices = _item_indices_from_text(raw_input)
        if not item_indices:
            item_indices = _leading_numbered_item_indices(raw_input)
        if item_indices:
            resolved = _resolve_current_item_indices(item_indices, field=field, report=report)
            if resolved.get("error"):
                return None
            resolved_field = str(resolved["field"])
            section_indices = list(resolved["item_indices"])
            return AgentAction(
                type="delete_item",
                field=resolved_field,
                item_indices=section_indices,
                requires_confirmation=len(section_indices) > 1,
                confirmation_message=_current_delete_confirmation(resolved_field, section_indices, report),
            )
        reference = _find_unique_item_reference(raw_input, report=report, field=field)
        if reference is not None:
            resolved_field, index, _item = reference
            return AgentAction(type="delete_item", field=resolved_field, item_indices=[index])
        if field in REPORT_FIELDS and _looks_like_clear_field(compact):
            return AgentAction(
                type="clear_field",
                field=field,
                requires_confirmation=True,
                confirmation_message=f"确认清空今天日报的“{_field_label(field)}”吗？",
            )
        return None

    append_value = _extract_append_value(raw_input)
    if append_value and field in REPORT_FIELDS:
        return AgentAction(type="append_items", field=field, items=[append_value])

    spelled_correction = _parse_spelled_correction_instruction(raw_input, report=report, field=field)
    if spelled_correction is not None:
        resolved_field, item_index, old_value, new_value = spelled_correction
        return AgentAction(
            type="replace_text",
            field=resolved_field,
            old_value=old_value,
            new_value=new_value,
            target_item_index=item_index,
        )

    change = _parse_change_instruction(raw_input)
    if change is None:
        return None
    left, right = change
    item_indices = _item_indices_from_text(raw_input)
    if item_indices and right:
        resolved = _resolve_current_item_indices(item_indices, field=field, report=report)
        if resolved.get("error"):
            return None
        resolved_field = str(resolved["field"])
        section_indices = list(resolved["item_indices"])
        if len(section_indices) != 1:
            return None
        old_value = (report.get(resolved_field, []) or [])[section_indices[0] - 1]
        return AgentAction(
            type="replace_text",
            field=resolved_field,
            old_value=old_value,
            new_value=right,
            target_item_index=section_indices[0],
        )
    if field in REPORT_FIELDS and (_left_mentions_field(left, field) or (focus_section in REPORT_FIELDS and not left)):
        replacement_items = _split_replacement_items(right)
        return AgentAction(
            type="replace_field",
            field=field,
            items=replacement_items,
            requires_confirmation=True,
            confirmation_message=f"确认把今天日报的“{_field_label(field)}”改成“{'；'.join(replacement_items)}”吗？",
        )
    if left and right:
        matched_field = _field_containing_text(report, left)
        if matched_field:
            return AgentAction(type="replace_text", field=matched_field, old_value=left, new_value=right)
    return None


def _looks_like_submit_request(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    return compact in {"确认提交", "提交", "提交日报", "没问题提交"} or ("提交" in compact and "日报" in compact)


def _looks_like_restore_recent_edit(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    return any(marker in compact for marker in ("撤销", "加回去", "加回来", "恢复", "不删了", "别删了", "不要删"))


def _current_batch_confirmation_message(actions: list[AgentAction], report: dict[str, list[str]]) -> str:
    confirming = [action for action in actions if action.requires_confirmation]
    if len(confirming) == 1:
        return confirming[0].confirmation_message or "这些操作需要确认，是否执行？"
    lines: list[str] = []
    for action in confirming:
        if action.type == "delete_item" and action.field in REPORT_FIELDS:
            lines.append(_current_delete_confirmation(action.field, action.item_indices, report))
        elif action.confirmation_message:
            lines.append(action.confirmation_message)
        else:
            lines.append("确认执行该操作吗？")
    return "这些操作需要确认：\n" + "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))


def _historical_full_report_replacement(
    full_report: dict[str, list[str]],
    *,
    target_date: str,
    focus_section: str,
    context: dict[str, Any],
) -> StateResolution:
    action = AgentAction(
        type="update_historical_report",
        field="none",
        target_date=target_date,
        source="replace_report",
        reference_report=full_report,
    )
    return StateResolution(
        plan=ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=[action],
            pending_interaction_to_set=PendingInteractionPlan(
                type="historical_report_edit_flow",
                operation="modify_report",
                target_field=focus_section if focus_section in REPORT_FIELDS else "none",
                context=_edit_flow_context(
                    context,
                    pending_type="historical_report_edit_flow",
                    target_date=target_date,
                    focused_section=focus_section,
                ),
            ),
            reason="Resolved a full report replacement inside historical report edit flow.",
        ),
        branch="historical_edit_replace_full_report",
    )


def _current_full_report_replacement(
    full_report: dict[str, list[str]],
    *,
    target_date: str,
    focus_section: str,
    context: dict[str, Any],
) -> StateResolution:
    actions = [
        AgentAction(type="replace_field", field="today_work", items=full_report["today_work"]),
        AgentAction(type="replace_field", field="problems", items=full_report["problems"]),
        AgentAction(type="replace_field", field="tomorrow_plan", items=full_report["tomorrow_plan"]),
    ]
    return StateResolution(
        plan=ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=actions,
            clear_pending_interaction=True,
            reason="Resolved a full report replacement inside current report edit flow.",
        ),
        branch="current_edit_replace_full_report",
    )


def _edit_flow_context(
    context: dict[str, Any],
    *,
    pending_type: str,
    target_date: str,
    focused_section: str,
) -> dict[str, Any]:
    next_context = dict(context)
    current_report = context.get("current_report") if isinstance(context.get("current_report"), dict) else {}
    if pending_type == "current_report_edit_flow":
        cursor = build_current_edit_cursor(
            target_date=target_date,
            current_report=current_report,
            focused_section=focused_section,
        )
    else:
        cursor = build_historical_edit_cursor(
            target_date=target_date,
            current_report=current_report,
            focused_section=focused_section,
        )
    next_context.update(
        {
            "target_date": target_date,
            "requested_action": context.get("requested_action") or "modify_report",
            "stage": "awaiting_field_edit_content" if focused_section in REPORT_FIELDS else "awaiting_edit_instruction",
            "current_report": current_report,
            "edit_cursor": cursor,
        }
    )
    if focused_section in REPORT_FIELDS:
        next_context["focus_section"] = focused_section
    return next_context


def _historical_flow_reprompt(
    message: str,
    *,
    target_date: str,
    focus_section: str,
    context: dict[str, Any],
) -> StateResolution:
    next_context = dict(context)
    next_context["target_date"] = target_date
    if focus_section in REPORT_FIELDS:
        next_context["focus_section"] = focus_section
        next_context["stage"] = "awaiting_field_edit_content"
    next_context["edit_cursor"] = build_historical_edit_cursor(
        target_date=target_date,
        current_report=context.get("current_report") if isinstance(context.get("current_report"), dict) else {},
        focused_section=focus_section,
    )
    return StateResolution(
        plan=ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user=message,
            pending_interaction_to_set=PendingInteractionPlan(
                type="historical_report_edit_flow",
                operation="modify_report",
                target_field=focus_section if focus_section in REPORT_FIELDS else "none",
                context=next_context,
            ),
            reason="Historical edit instruction was still missing a safe target.",
        ),
        branch="historical_edit_reprompt",
    )


def _current_flow_reprompt(
    message: str,
    *,
    target_date: str,
    focus_section: str,
    context: dict[str, Any],
) -> StateResolution:
    next_context = dict(context)
    next_context["target_date"] = target_date
    if focus_section in REPORT_FIELDS:
        next_context["focus_section"] = focus_section
        next_context["stage"] = "awaiting_field_edit_content"
    next_context["edit_cursor"] = build_current_edit_cursor(
        target_date=target_date,
        current_report=context.get("current_report") if isinstance(context.get("current_report"), dict) else {},
        focused_section=focus_section,
    )
    return StateResolution(
        plan=ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user=message,
            pending_interaction_to_set=PendingInteractionPlan(
                type="current_report_edit_flow",
                operation="modify_report",
                target_field=focus_section if focus_section in REPORT_FIELDS else "none",
                context=next_context,
            ),
            reason="Current edit instruction was still missing a safe target.",
        ),
        branch="current_edit_reprompt",
    )



def _split_compound_confirmation(raw_input: str) -> tuple[str | None, str | None]:
    text = str(raw_input or "").strip()
    if not text:
        return None, None
    prefixes = (
        "\u5bf9",
        "\u662f\u7684",
        "\u662f",
        "\u55ef",
        "\u6069",
        "\u53ef\u4ee5",
        "\u884c",
        "\u597d",
        "\u597d\u7684",
        "\u6ca1\u9519",
    )
    separators = ("\uff0c", ",", "\u3002", ";", "\uff1b", " ")
    for prefix in prefixes:
        for separator in separators:
            marker = prefix + separator
            if text.startswith(marker):
                rest = text[len(marker):].strip(" \t\r\n\u3000\uff0c,\u3002.\uff1b;\uff1a:")
                if rest:
                    return prefix, rest
        if text.startswith(prefix):
            rest = text[len(prefix):].strip(" \t\r\n\u3000\uff0c,\u3002.\uff1b;\uff1a:")
            if rest.startswith(("\u8fd8", "\u53e6\u5916", "\u4e5f", "\u518d", "\u8865\u5145")):
                return prefix, rest
    return None, None


def _confirmation_reply(raw_input: str) -> str | None:
    compact = _compact(raw_input)
    if not compact:
        return None
    confirm = {"对", "对的", "是", "是的", "嗯", "嗯嗯", "恩", "恩恩", "好", "好的", "没错", "确认", "确认删除", "确认清空", "确定", "可以", "行", "没问题", "需要", "删", "删除", "删吧", "删除吧", "撤回", "撤回并修改"}
    confirm.add("\u5220\u6389")
    cancel = {"不对", "不是", "取消", "算了", "不确认", "先不补", "不要", "不用", "不用了", "别删", "不删了", "不删除", "先不", "不需要", "撤销"}
    if compact in confirm:
        return "confirm"
    if compact.startswith(chr(0x786e) + chr(0x8ba4) + chr(0x5220) + chr(0x9664)) or compact.startswith(chr(0x786e) + chr(0x8ba4) + chr(0x6e05) + chr(0x7a7a)):
        return "confirm"
    if compact in cancel:
        return "cancel"
    pieces = [_compact(piece) for piece in re.split(r"[\s，。,.、；;：:！!？?（）()【】\[\]\"'“”‘’]+", raw_input)]
    pieces = [piece for piece in pieces if piece]
    if any(piece in confirm for piece in pieces):
        return "confirm"
    if any(piece in cancel for piece in pieces):
        return "cancel"
    if any(marker in compact for marker in ("不删", "不删除", "别删", "不要删", "取消", "撤销")):
        return "cancel"
    if len(compact) <= 6 and any(compact.endswith(marker) for marker in ("对", "确认", "确定")):
        return "confirm"
    return None


def _append_content_reply(raw_input: str) -> str | None:
    compact = _compact(raw_input)
    if not compact:
        return None
    finish = {"\u4e0d\u7528\u4e86", "\u5148\u4e0d\u7528", "\u4e0d\u586b\u4e86", "\u6ca1\u4e86", "\u6ca1\u6709\u4e86", "\u4e0d\u7528\u8865\u5145", "\u5148\u4e0d\u8865", "\u4e0d\u8865\u4e86"}
    if compact in finish:
        return "finish"
    return _confirmation_reply(raw_input)


def _quality_confirmation_reply(raw_input: str) -> str | None:
    compact = _compact(raw_input)
    if not compact:
        return None
    accept = {
        "就这么写",
        "就这样",
        "就这个",
        "就按这个",
        "按这个写",
        "按刚才的写",
        "原样写",
        "直接写",
        "不用补充",
        "不补充",
        "不用改",
        "不改了",
        "可以",
        "确认",
        "确定",
        "对",
        "是",
    }
    cancel = {"取消", "算了", "先不写", "不写了", "不要", "不用了"}
    if compact in accept:
        return "accept"
    if compact in cancel:
        return "cancel"
    return None


def _looks_like_display_request(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    short_commands = {
        "你先发我看看",
        "先发我看看",
        "发我看看",
        "发我看下",
        "发我一下",
        "给我看看",
        "给我看下",
        "展示",
        "展示一下",
        "展示给我看看",
        "看看",
        "看下",
        "先给我看看",
    }
    if compact in short_commands:
        return True
    query_terms = ("看", "发", "展示", "显示", "查")
    object_terms = ("日报", "日志", "复盘", "草稿", "内容")
    return any(term in compact for term in query_terms) and any(term in compact for term in object_terms)


def _looks_like_unsubmit_request(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    if "撤回" not in compact:
        return False
    return any(marker in compact for marker in ("日报", "提交", "已提交", "撤回"))


def _looks_like_new_unsubmit_intent(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if "撤回" not in compact:
        return False
    return any(marker in compact for marker in ("日报", "提交", "已提交"))


def _unsubmit_report_confirmation_plan(*, branch: str = "ask_unsubmit_report") -> StateResolution:
    return StateResolution(
        plan=ActionPlan(
            intent="system_action",
            confidence="high",
            should_write=True,
            clear_pending_interaction=True,
            actions=[AgentAction(type="unsubmit_report")],
            reason="Switched to explicit unsubmit report action.",
        ),
        branch=branch,
    )


def _pending_delete_target_completion(
    raw_input: str,
    *,
    operation: str,
    target_field: str,
    context: dict[str, Any],
) -> StateResolution | None:
    if operation not in {"delete_report_item", "delete_item"}:
        return None
    if not _pending_context_needs_delete_target(context):
        return None
    reference = _resolve_item_reference_from_context(raw_input, target_field=target_field, context=context)
    if reference is None:
        return None
    field, index, item = reference
    action = AgentAction(type="delete_item", field=field, item_indices=[index])
    pending_context = dict(context)
    pending_context.update(
        {
            "action": action.model_dump(),
            "item_indices": [index],
            "source_item_text": item,
        }
    )
    return StateResolution(
        plan=ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            clear_pending_interaction=True,
            actions=[action],
            reason="Completed pending delete target from item text reference.",
        ),
        branch="complete_pending_delete_target",
    )


def _pending_context_needs_delete_target(context: dict[str, Any]) -> bool:
    action = context.get("action") if isinstance(context.get("action"), dict) else {}
    indices = _clean_indices(context.get("item_indices") or action.get("item_indices"))
    action_type = str(action.get("type") or "")
    return action_type in {"", "delete_item"} and not indices


def _resolve_item_reference_from_context(
    raw_input: str,
    *,
    target_field: str,
    context: dict[str, Any],
) -> tuple[str, int, str] | None:
    report = _report_from_pending_context(context)
    return _find_unique_item_reference(raw_input, report=report, field=target_field)


def _report_from_pending_context(context: dict[str, Any]) -> dict[str, list[str]]:
    cursor = context.get("edit_cursor") if isinstance(context.get("edit_cursor"), dict) else None
    if cursor:
        return cursor_report(cursor)
    report = context.get("current_report") if isinstance(context.get("current_report"), dict) else {}
    return {
        field: _clean_candidate_items(report.get(field))
        for field in ("today_work", "problems", "tomorrow_plan")
    }


def _clean_candidate_items(value: Any) -> list[str]:
    candidates = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _dated_action_is_historical_edit(context: dict[str, Any], operation: str) -> bool:
    requested = str(context.get("requested_action") or operation or "modify_report")
    return requested in {"modify_report", "edit_report", "update_report", "modify", "edit", ""}


def _historical_edit_entry_message(target_date: str, context: dict[str, Any]) -> str:
    report = _historical_report(context)
    return (
        f"好的，接下来修改 {target_date} 的日报。当前内容：\n\n"
        f"今日工作：\n{_format_items_for_confirmation(report.get('today_work', []), empty_fallback='未填写')}\n\n"
        f"问题/风险：\n{_format_items_for_confirmation(report.get('problems', []), empty_fallback='暂无')}\n\n"
        f"明日计划：\n{_format_items_for_confirmation(report.get('tomorrow_plan', []), empty_fallback='未填写')}\n\n"
        "请直接说要修改哪一栏、哪一条，或直接粘贴完整日报内容。"
    )


def _historical_field_focus_message(field: str, context: dict[str, Any], *, repeated: bool = False) -> str:
    label = _field_label(field)
    current = _historical_field_text(field, context)
    target_date = str(context.get("target_date") or "昨天")
    if repeated:
        return (
            f"我已经定位到 {target_date} 日报的“{label}”部分了，目前内容是：{current}。\n"
            "请直接告诉我要改成什么，例如“改成暂无问题”或“补充对方反馈较慢”。"
        )
    return (
        f"{target_date} 日报的“{label}”目前是：\n\n{current}\n\n"
        "你想怎么改？可以直接说“改成暂无问题”“补充：对方反馈较慢”或“删除第1条”。"
    )


def _current_field_focus_message(field: str, context: dict[str, Any], *, repeated: bool = False) -> str:
    label = _field_label(field)
    current = _historical_field_text(field, context)
    if repeated:
        return (
            f"我已经定位到今天日报的“{label}”部分了，目前内容是：{current}。\n"
            "请直接告诉我要改成什么，例如“改成暂无问题”或“补充对方反馈较慢”。"
        )
    return (
        f"今天日报的“{label}”目前是：\n\n{current}\n\n"
        "你想怎么改？可以直接说“改成暂无问题”“补充：对方反馈较慢”或“删除第1条”。"
    )


def _historical_field_text(field: str, context: dict[str, Any]) -> str:
    report = context.get("current_report") if isinstance(context.get("current_report"), dict) else {}
    values = report.get(field)
    if not isinstance(values, list):
        values = []
    cleaned = [str(item).strip() for item in values if str(item or "").strip()]
    if not cleaned:
        return "暂无"
    if len(cleaned) == 1:
        return cleaned[0]
    return "\n".join(f"{index}. {item}" for index, item in enumerate(cleaned, start=1))


def _historical_report(context: dict[str, Any]) -> dict[str, list[str]]:
    cursor = context.get("edit_cursor") if isinstance(context.get("edit_cursor"), dict) else None
    if cursor:
        return cursor_report(cursor)
    report = context.get("current_report") if isinstance(context.get("current_report"), dict) else {}
    return {
        field: _clean_candidate_items(report.get(field))
        for field in ("today_work", "problems", "tomorrow_plan")
    }


def _parse_full_report_replacement(raw_input: str) -> dict[str, list[str]] | None:
    text = str(raw_input or "").strip()
    if not text:
        return None
    structured = _parse_labeled_report(text)
    if _looks_like_full_report(structured):
        return structured
    prose = _parse_prose_report(text)
    if _looks_like_full_report(prose):
        return prose
    return None


REPORT_SECTION_HEADING_RE = re.compile(
    r"^\s*(今日工作(?:完成情况)?|今日完成(?:工作)?|今天工作|今天完成|工作完成情况|已完成|完成工作|问题[/／]风险|问题风险|风险问题|存在问题|遇到的问题|问题|风险|困难|阻碍|blocker|明日计划|明天计划|明日安排|明天安排|明日要做|明天要做|接下来计划|后续计划)\s*[：: ]?\s*(.*)$",
    re.MULTILINE | re.IGNORECASE,
)


def _parse_labeled_report(text: str) -> dict[str, list[str]]:
    matches = list(REPORT_SECTION_HEADING_RE.finditer(text))
    sections: dict[str, list[str]] = {"today_work": [], "problems": [], "tomorrow_plan": []}
    if not matches:
        return sections
    for index, match in enumerate(matches):
        field = _field_from_heading(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = "\n".join(part for part in (match.group(2), text[start:end]) if part)
        sections[field].extend(_split_report_items(content, field=field))
    return sections


def _parse_prose_report(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"today_work": [], "problems": [], "tomorrow_plan": []}
    for sentence in re.split(r"[。！？!?；;\n]+", text):
        sentence = _strip_side(sentence)
        if not sentence:
            continue
        before_plan, plan_text = _split_tomorrow_plan(sentence)
        if before_plan:
            _append_work_or_problem(sections, before_plan)
        if plan_text:
            sections["tomorrow_plan"].append(_normalize_tomorrow_item(plan_text))
    sections["today_work"] = _dedupe_keep_order(sections["today_work"])
    sections["problems"] = _dedupe_keep_order(sections["problems"])
    sections["tomorrow_plan"] = _dedupe_keep_order(sections["tomorrow_plan"])
    return sections


def _split_tomorrow_plan(sentence: str) -> tuple[str, str]:
    match = re.search(r"(明天计划|明日计划|明天安排|明日安排|明天|明日|计划)(.+)$", sentence)
    if not match:
        return sentence, ""
    before = sentence[: match.start()]
    plan = match.group(0)
    return _strip_side(before), _strip_side(plan)


def _append_work_or_problem(sections: dict[str, list[str]], text: str) -> None:
    if _looks_like_problem_text(text):
        cleaned = _normalize_problem_item(text)
        if cleaned:
            sections["problems"].append(cleaned)
        return
    sections["today_work"].extend(_split_report_items(text, field="today_work"))


def _looks_like_problem_text(text: str) -> bool:
    compact = _compact(text)
    problem_markers = ("发现", "存在", "问题", "风险", "困难", "未签", "没签", "没有签", "缺少", "缺失", "异常", "卡住")
    return any(marker in compact for marker in problem_markers)


def _split_report_items(content: str, *, field: str) -> list[str]:
    text = _strip_side(content)
    if not text:
        return []
    parts = re.split(r"(?:\n+|[，,、]|同时|并且|以及|另外|还有)", text)
    result: list[str] = []
    for part in parts:
        cleaned = _strip_side(part)
        if not cleaned:
            continue
        cleaned = re.sub(r"^\s*(?:\d+|[一二三四五六七八九十]+)[、.．]\s*", "", cleaned).strip()
        cleaned = re.sub(r"^\s*[（(](?:\d+|[一二三四五六七八九十]+)[）)]\s*", "", cleaned).strip()
        if field == "today_work":
            cleaned = _normalize_work_item(cleaned)
        elif field == "problems":
            cleaned = _normalize_problem_item(cleaned)
        elif field == "tomorrow_plan":
            cleaned = _normalize_tomorrow_item(cleaned)
        if cleaned:
            result.append(cleaned)
    return _dedupe_keep_order(result)


def _field_from_heading(heading: str) -> str:
    compact = _compact(heading)
    if "明" in compact or "计划" in compact or "安排" in compact:
        return "tomorrow_plan"
    if "问题" in compact or "风险" in compact:
        return "problems"
    return "today_work"


def _normalize_work_item(text: str) -> str:
    cleaned = _strip_side(text)
    cleaned = re.sub(r"^(我)?(今天|今日)", "", cleaned).strip()
    cleaned = cleaned.replace("去了", "去")
    cleaned = re.sub(r"审核了", "审核", cleaned)
    cleaned = re.sub(r"写了", "撰写", cleaned)
    cleaned = re.sub(r"处理了", "处理", cleaned)
    return _strip_side(cleaned)


def _normalize_problem_item(text: str) -> str:
    cleaned = _strip_side(text)
    cleaned = re.sub(r"^(问题[/／]风险|问题风险|风险问题|存在问题|遇到的问题|问题|风险|困难|阻碍|blocker)[：:\s]*", "", cleaned, flags=re.IGNORECASE)
    compact = _compact(cleaned).lower()
    if compact in {"无", "暂无", "无风险", "暂无风险", "无问题", "暂无问题", "没有风险", "没有问题", "没风险", "没问题", "noissues", "noblockers"}:
        return "暂无明显问题"
    cleaned = cleaned.replace("没签", "未签").replace("没有签", "未签")
    return _strip_side(cleaned)


def _normalize_tomorrow_item(text: str) -> str:
    cleaned = _strip_side(text)
    cleaned = re.sub(r"^(明天计划|明日计划|明天安排|明日安排|明日要做|明天要做|接下来计划|后续计划|明天|明日|计划)[：:\s]*", "", cleaned)
    return _strip_side(cleaned)


def _looks_like_full_report(sections: dict[str, list[str]]) -> bool:
    filled = [field for field in ("today_work", "problems", "tomorrow_plan") if sections.get(field)]
    total_items = sum(len(sections.get(field, [])) for field in ("today_work", "problems", "tomorrow_plan"))
    total_chars = sum(
        len(item)
        for field in ("today_work", "problems", "tomorrow_plan")
        for item in sections.get(field, [])
    )
    return len(filled) >= 2 and bool(sections.get("today_work")) and (total_items >= 3 or total_chars >= 40)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = _compact(value)
        if value and compact and compact not in seen:
            result.append(value)
            seen.add(compact)
    return result


def _replace_report_confirmation(target_date: str, report: dict[str, list[str]]) -> str:
    return (
        f"确认用这段内容整体替换 {target_date} 日报吗？\n\n"
        f"今日工作：\n{_format_items_for_confirmation(report['today_work'])}\n\n"
        f"问题/风险：\n{_format_items_for_confirmation(report['problems'], empty_fallback='暂无')}\n\n"
        f"明日计划：\n{_format_items_for_confirmation(report['tomorrow_plan'])}"
    )


def _format_items_for_confirmation(values: list[str], *, empty_fallback: str = "未填写") -> str:
    cleaned = [str(item).strip() for item in values if str(item or "").strip()]
    if not cleaned:
        return empty_fallback
    return "\n".join(f"{index}. {item}" for index, item in enumerate(cleaned, start=1))


def _looks_like_delete_or_clear(compact: str) -> bool:
    return any(marker in compact for marker in ("删除", "删掉", "删了", "去掉", "移除", "清空", "清掉", "不要", "全删", "全删了"))


def _looks_like_clear_field(compact: str) -> bool:
    return any(marker in compact for marker in ("清空", "清掉", "全删", "全部删", "都删", "整个", "整栏"))


def _item_indices_from_text(raw_input: str) -> list[int]:
    result: list[int] = []
    number = r"\d{1,3}|[一二两三四五六七八九十]{1,3}"
    for start, end in re.findall(rf"第?({number})(?:条|项|个)?(?:到|至|~|～|-|—|－)第?({number})(?:条|项|个)?", raw_input):
        start_num = _parse_cn_number(start)
        end_num = _parse_cn_number(end)
        if start_num is None or end_num is None:
            continue
        low, high = sorted((start_num, end_num))
        result.extend(range(low, high + 1))
    for raw in re.findall(r"第([一二两三四五六七八九十\d]+)(?:条|个|项)", raw_input):
        parsed = _parse_cn_number(raw)
        if parsed is not None:
            result.append(parsed)
    return _dedupe_numbers(result)


def _dedupe_numbers(values: list[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _leading_numbered_item_indices(raw_input: str) -> list[int]:
    result: list[int] = []
    for raw in re.findall(r"(?:^|[，,。；;\n\r\s])([一二两三四五六七八九十\d]+)[.、．]\s*", raw_input):
        parsed = _parse_cn_number(raw)
        if parsed is not None:
            result.append(parsed)
    return result


def _parse_cn_number(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    numerals = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if text in numerals:
        return numerals[text]
    if text.startswith("十") and len(text) == 2 and text[1] in numerals:
        return 10 + numerals[text[1]]
    if text.endswith("十") and len(text) == 2 and text[0] in numerals:
        return numerals[text[0]] * 10
    if "十" in text:
        left, right = text.split("十", 1)
        left_value = numerals.get(left, 1 if not left else 0)
        right_value = numerals.get(right, 0) if right else 0
        return left_value * 10 + right_value
    return None


def _resolve_historical_item_indices(
    item_indices: list[int],
    *,
    field: str,
    report: dict[str, list[str]],
) -> dict[str, Any]:
    if not item_indices:
        return {"error": "请说明要处理第几条。"}
    if any(index <= 0 for index in item_indices):
        return {"error": "条目序号需要从第1条开始。"}
    if field in REPORT_FIELDS:
        values = report.get(field, [])
        max_index = max(item_indices)
        if max_index > len(values):
            return {"error": f"昨天日报的“{_field_label(field)}”没有第{max_index}条。"}
        return {"field": field, "item_indices": item_indices}
    flat: list[tuple[str, int]] = []
    for candidate_field in ("today_work", "problems", "tomorrow_plan"):
        for section_index, _item in enumerate(report.get(candidate_field, []), start=1):
            flat.append((candidate_field, section_index))
    max_index = max(item_indices)
    if max_index > len(flat):
        return {"error": f"昨天日报没有第{max_index}条。"}
    selected = [flat[index - 1] for index in item_indices]
    selected_fields = {item[0] for item in selected}
    if len(selected_fields) != 1:
        return {"error": "这几个序号跨了不同栏目，请分开操作。"}
    selected_field = selected[0][0]
    return {"field": selected_field, "item_indices": [item[1] for item in selected]}


def _resolve_current_item_indices(
    item_indices: list[int],
    *,
    field: str,
    report: dict[str, list[str]],
) -> dict[str, Any]:
    resolved = _resolve_historical_item_indices(item_indices, field=field, report=report)
    if "error" not in resolved:
        return resolved
    error = str(resolved["error"])
    return {"error": error.replace("昨天日报", "今天日报")}


def _historical_delete_confirmation(
    target_date: str,
    field: str,
    item_indices: list[int],
    report: dict[str, list[str]],
) -> str:
    label = _field_label(field)
    values = report.get(field, [])
    if len(item_indices) == 1 and 1 <= item_indices[0] <= len(values):
        return f"你确认要删除 {target_date} 日报“{label}”第{item_indices[0]}条“{values[item_indices[0] - 1]}”这条吗？回复“确认”执行，回复“取消”保留。"
    return _delete_items_confirmation_message(f"{target_date} 日报“{label}”", item_indices, values)


def _current_delete_confirmation(
    field: str,
    item_indices: list[int],
    report: dict[str, list[str]],
) -> str:
    label = _field_label(field)
    values = report.get(field, [])
    if len(item_indices) == 1 and 1 <= item_indices[0] <= len(values):
        return f"你确认要删除今天日报“{label}”第{item_indices[0]}条“{values[item_indices[0] - 1]}”这条吗？回复“确认”执行，回复“取消”保留。"
    return _delete_items_confirmation_message(f"今天日报“{label}”", item_indices, values)


def _delete_items_confirmation_message(scope_label: str, item_indices: list[int], values: list[str]) -> str:
    valid = [index for index in item_indices if 1 <= index <= len(values)]
    if valid:
        lines = [f"你确认要删除{scope_label}以下 {len(valid)} 条吗？"]
        lines.extend(f"- 第{index}条“{values[index - 1]}”" for index in valid[:8])
        if len(valid) > 8:
            lines.append(f"- 另有 {len(valid) - 8} 条")
        lines.append("回复“确认”执行，回复“取消”保留。")
        return "\n".join(lines)
    joined = "、".join(str(index) for index in item_indices)
    return f"你确认要删除{scope_label}第{joined}条吗？回复“确认”执行，回复“取消”保留。"


def _historical_replace_item_confirmation(
    target_date: str,
    field: str,
    item_indices: list[int],
    replacement: str,
    report: dict[str, list[str]],
) -> str:
    label = _field_label(field)
    values = report.get(field, [])
    if len(item_indices) == 1 and 1 <= item_indices[0] <= len(values):
        return f"确认把 {target_date} 日报“{label}”第{item_indices[0]}条“{values[item_indices[0] - 1]}”改成“{replacement}”吗？"
    joined = "、".join(str(index) for index in item_indices)
    return f"确认把 {target_date} 日报“{label}”第{joined}条改成“{replacement}”吗？"


def _current_replace_item_confirmation(
    field: str,
    item_indices: list[int],
    replacement: str,
    report: dict[str, list[str]],
) -> str:
    label = _field_label(field)
    values = report.get(field, [])
    if len(item_indices) == 1 and 1 <= item_indices[0] <= len(values):
        return f"确认把今天日报“{label}”第{item_indices[0]}条“{values[item_indices[0] - 1]}”改成“{replacement}”吗？"
    joined = "、".join(str(index) for index in item_indices)
    return f"确认把今天日报“{label}”第{joined}条改成“{replacement}”吗？"


def _extract_append_value(raw_input: str) -> str:
    match = re.search(r"(?:补充|添加|增加|加上|补上)[：:\s]*(.+)$", raw_input)
    if not match:
        return ""
    return _strip_side(match.group(1))


def _parse_spelled_correction_instruction(
    raw_input: str,
    *,
    report: dict[str, list[str]],
    field: str,
) -> tuple[str, int, str, str] | None:
    prefix_correction = _parse_spelled_prefix_correction(raw_input, report=report, field=field)
    if prefix_correction is not None:
        return prefix_correction
    matches = re.findall(r"([\u4e00-\u9fffA-Za-z0-9])\s*\u662f\s*[^，,。；;\n]{1,12}?\u7684\s*([\u4e00-\u9fffA-Za-z0-9])", raw_input)
    if not matches:
        return None
    intended = "".join(replacement for _spoken, replacement in matches).strip()
    spoken = "".join(spoken_char for spoken_char, _replacement in matches).strip()
    if not intended:
        return None
    item_indices = _item_indices_from_text(raw_input) or _leading_numbered_item_indices(raw_input)
    reference = _resolve_spelled_correction_reference(item_indices, report=report, field=field)
    if reference is None:
        reference = _find_unique_item_reference(raw_input, report=report, field=field)
    if reference is None:
        return None
    resolved_field, item_index, old_value = reference
    new_value = _apply_spelled_correction(old_value, intended=intended, spoken=spoken)
    if not new_value or _compact(new_value) == _compact(old_value):
        return None
    return resolved_field, item_index, old_value, new_value


def _parse_local_text_delete_instruction(
    raw_input: str,
    *,
    report: dict[str, list[str]],
    field: str,
) -> tuple[str, int, str, str] | None:
    compact = _compact(raw_input)
    if not compact or not any(marker in compact for marker in ("\u5220\u6389", "\u5220\u9664", "\u5220\u4e86", "\u53bb\u6389", "\u79fb\u9664", "\u4e0d\u8981")):
        return None
    if "\u62ec\u53f7" in compact:
        bracket_reference = _find_unique_bracket_item(report, field=field)
        if bracket_reference is not None:
            resolved_field, item_index, old_value = bracket_reference
            new_value = re.sub(r"[\uff08(][^\uff09)]*[\uff09)]", "", old_value)
            new_value = re.sub(r"\s{2,}", " ", new_value).strip()
            if new_value and _compact(new_value) != _compact(old_value):
                return resolved_field, item_index, old_value, new_value
    target_text = _extract_local_delete_target_text(raw_input)
    if not target_text:
        return None
    reference = _find_fuzzy_text_reference(target_text, report=report, field=field)
    if reference is None:
        return None
    resolved_field, item_index, old_value, matched_text = reference
    new_value = old_value.replace(matched_text, "", 1).strip(" \t\r\n\uff0c,.;\u3002\uff1b")
    if not new_value or _compact(new_value) == _compact(old_value):
        return None
    return resolved_field, item_index, old_value, new_value


def _find_unique_bracket_item(
    report: dict[str, list[str]],
    *,
    field: str,
) -> tuple[str, int, str] | None:
    fields = [field] if field in REPORT_FIELDS else ["today_work", "problems", "tomorrow_plan"]
    matches: list[tuple[str, int, str]] = []
    for candidate_field in fields:
        for index, item in enumerate(report.get(candidate_field, []), start=1):
            if re.search(r"[\uff08(][^\uff09)]*[\uff09)]", str(item or "")):
                matches.append((candidate_field, index, item))
    return matches[0] if len(matches) == 1 else None


def _extract_local_delete_target_text(raw_input: str) -> str:
    text = str(raw_input or "").strip()
    patterns = [
        r"(.{1,80}?)(?:\u8fd9\u51e0\u4e2a\u5b57|\u8fd9\u51e0\u4e2a|\u8fd9\u51e0\u5b57|\u8fd9\u4e9b\u5b57|\u51e0\u4e2a\u5b57)(?:\u5220\u6389|\u5220\u9664|\u5220\u4e86|\u53bb\u6389|\u79fb\u9664)",
        r"(.{1,80}?)(?:\u5220\u6389|\u5220\u9664|\u5220\u4e86|\u53bb\u6389|\u79fb\u9664)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = _strip_side(match.group(1))
        candidate = re.sub(r"^(?:\u95ee\u9898\u4e0e\u98ce\u9669|\u95ee\u9898/\u98ce\u9669|\u95ee\u9898|\u98ce\u9669|\u4eca\u65e5\u5de5\u4f5c|\u4eca\u5929\u5de5\u4f5c|\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212)(?:\u91cc\u9762|\u91cc|\u4e2d|:|\uff1a)?", "", candidate)
        if candidate:
            return candidate
    return ""


def _find_fuzzy_text_reference(
    target_text: str,
    *,
    report: dict[str, list[str]],
    field: str,
) -> tuple[str, int, str, str] | None:
    target_key = _compact(target_text)
    if not target_key:
        return None
    fields = [field] if field in REPORT_FIELDS else ["today_work", "problems", "tomorrow_plan"]
    matches: list[tuple[float, str, int, str, str]] = []
    for candidate_field in fields:
        for index, item in enumerate(report.get(candidate_field, []), start=1):
            item_text = str(item or "")
            item_key = _compact(item_text)
            if not item_key:
                continue
            if target_key in item_key:
                start = item_key.find(target_key)
                matched = _substring_by_compact_window(item_text, item_key, start, len(target_key)) or target_text
                matches.append((1.0, candidate_field, index, item_text, matched))
                continue
            for width in range(max(2, len(target_key) - 2), min(len(item_key), len(target_key) + 2) + 1):
                for start in range(0, len(item_key) - width + 1):
                    window = item_key[start : start + width]
                    score = SequenceMatcher(None, target_key, window).ratio()
                    if score >= 0.78:
                        matched = _substring_by_compact_window(item_text, item_key, start, width)
                        if matched:
                            matches.append((score, candidate_field, index, item_text, matched))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    best_score = matches[0][0]
    best = [item for item in matches if item[0] == best_score]
    if len({(item[1], item[2], item[4]) for item in best}) != 1:
        return None
    _score, resolved_field, item_index, old_value, matched_text = best[0]
    return resolved_field, item_index, old_value, matched_text


def _substring_by_compact_window(original: str, compact_original: str, start: int, width: int) -> str:
    if _compact(original) != compact_original:
        return ""
    return original[start : start + width]


def _parse_spelled_prefix_correction(
    raw_input: str,
    *,
    report: dict[str, list[str]],
    field: str,
) -> tuple[str, int, str, str] | None:
    match = re.search(r"(?:\u5c31\u662f|\u662f|=|:|\uff1a)\s*([\u4e00-\u9fffA-Za-z0-9]{2,12}?)([\u4e00-\u9fffA-Za-z0-9])\s*\u662f\s*[^，,。；;\n]{1,12}?\u7684\s*\2", raw_input)
    if not match:
        return None
    prefix = match.group(1).strip()
    if len(prefix) < 2:
        return None
    item_indices = _item_indices_from_text(raw_input) or _leading_numbered_item_indices(raw_input)
    ordinal = item_indices[0] if len(item_indices) == 1 else None
    candidates = _spelled_prefix_candidates(prefix, report=report, fields=[field] if field in REPORT_FIELDS else ["today_work", "problems", "tomorrow_plan"])
    if field in REPORT_FIELDS and (not candidates or (ordinal is not None and ordinal > len(candidates))):
        candidates = _spelled_prefix_candidates(prefix, report=report, fields=["today_work", "problems", "tomorrow_plan"])
    if ordinal is not None and 1 <= ordinal <= len(candidates):
        resolved_field, item_index, old_value = candidates[ordinal - 1]
    elif len(candidates) == 1:
        resolved_field, item_index, old_value = candidates[0]
    else:
        return None
    width = len(prefix)
    if len(old_value) < width:
        return None
    new_value = prefix + old_value[width:]
    if _compact(new_value) == _compact(old_value):
        return None
    return resolved_field, item_index, old_value, new_value


def _spelled_prefix_candidates(
    prefix: str,
    *,
    report: dict[str, list[str]],
    fields: list[str],
) -> list[tuple[str, int, str]]:
    candidates: list[tuple[str, int, str]] = []
    for candidate_field in fields:
        for index, item in enumerate(report.get(candidate_field, []), start=1):
            text = str(item or "")
            if text and text[0] == prefix[0] and not text.startswith(prefix):
                candidates.append((candidate_field, index, text))
    return candidates


def _resolve_spelled_correction_reference(
    item_indices: list[int],
    *,
    report: dict[str, list[str]],
    field: str,
) -> tuple[str, int, str] | None:
    if len(item_indices) != 1:
        return None
    index = item_indices[0]
    if index <= 0:
        return None
    if field in REPORT_FIELDS:
        values = report.get(field, [])
        if 1 <= index <= len(values):
            return field, index, values[index - 1]
        return None
    candidates: list[tuple[str, int, str]] = []
    for candidate_field in ("today_work", "problems", "tomorrow_plan"):
        values = report.get(candidate_field, [])
        if 1 <= index <= len(values):
            candidates.append((candidate_field, index, values[index - 1]))
    if len(candidates) == 1:
        return candidates[0]
    return None


def _apply_spelled_correction(old_value: str, *, intended: str, spoken: str) -> str:
    text = str(old_value or "")
    if not text or not intended or intended in text:
        return text
    width = len(intended)
    if width <= 0 or width > len(text):
        return text
    starts: list[int] = []
    if spoken:
        first = spoken[0]
        starts.extend(index for index, char in enumerate(text) if char == first)
    starts.extend(range(0, len(text) - width + 1))
    seen: set[int] = set()
    for start in starts:
        if start in seen or start < 0 or start + width > len(text):
            continue
        seen.add(start)
        current = text[start : start + width]
        if current and current != intended:
            return text[:start] + intended + text[start + width :]
    return text


def _parse_change_instruction(raw_input: str) -> tuple[str, str] | None:
    match = re.match(r"^(.{1,80}?)(?:改成|改为|换成|更正为|修改为|更新为)(.{1,120})$", raw_input.strip())
    if match:
        left = _strip_side(match.group(1))
        right = _strip_side(match.group(2))
    else:
        match = re.match(r"^(?:改成|改为|换成|更正为|修改为|更新为)(.{1,120})$", raw_input.strip())
        if not match:
            return None
        left = ""
        right = _strip_side(match.group(1))
    if not right:
        return None
    return left, right


def _left_mentions_field(left: str, field: str) -> bool:
    compact = _compact(left)
    if field == "today_work":
        return any(marker in compact for marker in ("今日工作", "今天工作", "工作", "完成"))
    if field == "problems":
        return any(marker in compact for marker in ("问题", "风险"))
    if field == "tomorrow_plan":
        return any(marker in compact for marker in ("明日计划", "明天计划", "明天", "计划"))
    return False


def _split_replacement_items(value: str) -> list[str]:
    cleaned = _strip_side(value)
    if not cleaned:
        return []
    parts = re.split(r"(?:\n+|[；;])", cleaned)
    result = [_strip_side(part) for part in parts if _strip_side(part)]
    return result or [cleaned]


def _field_containing_text(report: dict[str, list[str]], text: str) -> str | None:
    needle = _compact(text)
    if not needle:
        return None
    matches: list[str] = []
    for field in ("today_work", "problems", "tomorrow_plan"):
        for item in report.get(field, []):
            item_key = _compact(item)
            if needle and item_key and (needle in item_key or item_key in needle):
                matches.append(field)
                break
    return matches[0] if len(matches) == 1 else None


def _find_unique_item_reference(
    raw_input: str,
    *,
    report: dict[str, list[str]],
    field: str = "none",
) -> tuple[str, int, str] | None:
    needle = _item_reference_needle(raw_input)
    if not needle:
        return None
    fields = [field] if field in REPORT_FIELDS else ["today_work", "problems", "tomorrow_plan"]
    matches: list[tuple[str, int, str]] = []
    for candidate_field in fields:
        for index, item in enumerate(report.get(candidate_field, []), start=1):
            item_key = _compact(item)
            if needle and item_key and (needle in item_key or item_key in needle):
                matches.append((candidate_field, index, item))
    return matches[0] if len(matches) == 1 else None


def _item_reference_needle(raw_input: str) -> str:
    compact = _compact(raw_input)
    for marker in (
        "刚才那条",
        "刚才那个",
        "这一条",
        "这条",
        "那条",
        "这个",
        "那个",
        "算了",
        "还是",
        "删掉",
        "删除",
        "删了",
        "删",
        "去掉",
        "移除",
        "吧",
    ):
        compact = compact.replace(marker, "")
    return compact


def _strip_side(value: str) -> str:
    return str(value or "").strip(" ：:，,。；;！!？?“”\"'")


def _field_label(field: str) -> str:
    return {
        "today_work": "今日工作",
        "problems": "问题/风险",
        "tomorrow_plan": "明日计划",
    }.get(field, "日报")


def _confirmed_action_plan(*, operation: str, target_field: str, context: dict[str, Any]) -> ActionPlan | None:
    actions_from_context = _actions_from_context(context)
    if actions_from_context:
        confirmed_actions: list[AgentAction] = []
        for action in actions_from_context:
            action_payload = action.model_dump()
            action_payload["requires_confirmation"] = False
            action_payload["confirmation_message"] = ""
            confirmed_actions.append(AgentAction.model_validate(action_payload))
        pending_to_restore = None if operation in {"batch_action", "withdraw_and_modify"} else _pending_plan_from_resume_context(context)
        intent = "confirm_submit" if len(confirmed_actions) == 1 and confirmed_actions[0].type == "submit_report" else "edit_draft"
        return ActionPlan(
            intent=intent,
            confidence="high",
            should_write=True,
            clear_pending_interaction=pending_to_restore is None,
            pending_interaction_to_set=pending_to_restore,
            actions=confirmed_actions,
            reason=f"Confirmed pending {operation or 'batch'} actions.",
        )

    action_from_context = _action_from_context(context)
    if action_from_context is not None:
        action_payload = action_from_context.model_dump()
        action_payload["requires_confirmation"] = False
        action_payload["confirmation_message"] = ""
        action_from_context = AgentAction.model_validate(action_payload)
        pending_to_restore = _pending_plan_from_resume_context(context)
        return ActionPlan(
            intent="edit_draft" if action_from_context.type != "submit_report" else "confirm_submit",
            confidence="high",
            should_write=True,
            clear_pending_interaction=pending_to_restore is None,
            pending_interaction_to_set=pending_to_restore,
            actions=[action_from_context],
            reason=f"Confirmed pending {operation or action_from_context.type} action.",
        )
    if operation in {"delete_report_item", "delete_item"} and target_field in {"today_work", "problems", "tomorrow_plan"}:
        item_indices = _clean_indices(context.get("item_indices") or context.get("indices") or context.get("item_index"))
        if not item_indices:
            return ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=False,
                clear_pending_interaction=False,
                reply_to_user="没有找到要删除的条目，请重新说明要删除哪一项。",
                reason="Pending delete action missing item indices.",
            )
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            clear_pending_interaction=True,
            actions=[AgentAction(type="delete_item", field=target_field, item_indices=item_indices)],
            reason="Confirmed pending delete action.",
        )
    if operation in {"submit_report", "confirm_submit"}:
        return ActionPlan(
            intent="confirm_submit",
            confidence="high",
            should_write=True,
            clear_pending_interaction=True,
            actions=[AgentAction(type="submit_report")],
            reason="Confirmed pending submit action.",
        )
    if operation in {"query_current", "current_report_query"}:
        return ActionPlan(
            intent="query_current",
            confidence="high",
            should_write=False,
            clear_pending_interaction=True,
            reason="Confirmed pending current report query.",
        )
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=False,
        clear_pending_interaction=True,
        reply_to_user="好的，这一步我先不继续了。你可以直接说要查看、修改或补充哪一项。",
        reason=f"Unsupported pending action operation: {operation}",
    )


def _pending_plan_from_resume_context(context: dict[str, Any]) -> PendingInteractionPlan | None:
    resume = context.get("resume_pending_interaction")
    if isinstance(resume, dict):
        resume = with_edit_cursor(resume)
        if resume.get("type") in {"historical_report_edit_flow", "current_report_edit_flow"}:
            return PendingInteractionPlan.model_validate(resume)

    cursor = context.get("edit_cursor") if isinstance(context.get("edit_cursor"), dict) else None
    pending_type = "current_report_edit_flow" if cursor and cursor.get("mode") == "current_edit" else "historical_report_edit_flow"
    normalized = normalize_edit_cursor({"type": pending_type, "context": {"edit_cursor": cursor}}) if cursor else None
    if normalized is None:
        return None
    field = cursor_focused_section(normalized)
    pending = {
        "type": pending_type,
        "operation": "modify_report",
        "target_field": field,
        "context": {
            "target_date": cursor_target_date(normalized),
            "requested_action": "modify_report",
            "stage": normalized.get("stage") or "awaiting_edit_instruction",
            "focus_section": field,
            "current_report": normalized.get("active_draft_snapshot") or {},
            "edit_cursor": normalized,
        },
        "expires_after_turns": 12,
    }
    return PendingInteractionPlan.model_validate(pending)


def _action_from_context(context: dict[str, Any]) -> AgentAction | None:
    payload = context.get("action")
    if not isinstance(payload, dict):
        return None
    try:
        return AgentAction.model_validate(payload)
    except Exception:
        return None


def _actions_from_context(context: dict[str, Any]) -> list[AgentAction]:
    payload = context.get("actions")
    if not isinstance(payload, list):
        return []
    actions: list[AgentAction] = []
    for item in payload:
        if not isinstance(item, dict):
            return []
        try:
            actions.append(AgentAction.model_validate(item))
        except Exception:
            return []
    return actions


def _clean_indices(value: Any) -> list[int]:
    candidates = value if isinstance(value, list) else [value]
    result: list[int] = []
    for item in candidates:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            result.append(number)
    return result


def _cancel_message(operation: str) -> str:
    if operation in {"delete_report_item", "delete_item"}:
        return "好的，已取消删除。"
    if operation == "high_risk_clear_all":
        return "好的，已取消清空，当前日报内容已保留。"
    if operation in {"batch_action", "withdraw_and_modify"}:
        return "好的，已取消本次修改。"
    if operation in {"submit_report", "confirm_submit"}:
        return "好的，暂不提交。"
    return "好的，已取消本次操作。"


def _append_content_message(field: str) -> str:
    if field == "today_work":
        return "好的，请说要补充的今日工作内容。"
    if field == "problems":
        return "好的，请说要补充的问题内容。"
    if field == "tomorrow_plan":
        return "好的，请说要补充的明日计划内容。"
    return "好的，请说要补充的具体内容。"


def _resolve_report_field_choice(raw_input: str) -> str | None:
    compact = _compact(raw_input)
    if not compact:
        return None
    scores = {
        "today_work": _label_score(compact, ("今日工作", "今天工作", "工作", "完成")),
        "problems": _label_score(compact, ("问题", "风险")),
        "tomorrow_plan": _label_score(compact, ("明日计划", "明天计划", "明天", "计划")),
    }
    best_field, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score <= 0:
        return None
    competing = [score for field, score in scores.items() if field != best_field]
    if competing and best_score == max(competing):
        return None
    return best_field


def _looks_like_field_selection_only(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    if any(marker in compact for marker in ("还会", "还要", "另外", "同时", "顺便", "还得")):
        return False
    if any(marker in compact for marker in ("改", "修改", "补充", "添加", "增加", "删", "删除", "清空", "换成", "替换", "改成", "改为")):
        return False
    if len(compact) > 8:
        return False
    return _resolve_report_field_choice(compact) is not None


def _mentions_current_day_report(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    if any(marker in compact for marker in ("改成", "改为", "换成", "修改为", "更新为", "更正为")):
        return any(marker in compact for marker in ("今天的日报", "今日日报", "当前日报", "现在的日报", "当前草稿"))
    if any(marker in compact for marker in ("今天", "今日", "当前", "现在")) and any(
        marker in compact for marker in ("日报", "日志", "复盘", "草稿", "今日工作", "问题", "风险", "明日计划")
    ):
        return True
    return any(marker in compact for marker in ("今天", "今日", "刚才", "上午", "下午", "晚上")) and any(
        marker in compact for marker in ("做了", "完成", "处理", "审核", "发送", "沟通", "整理", "推进", "跟进", "记录")
    )


def _mentions_previous_day_report(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    return any(marker in compact for marker in ("昨天", "昨日", "前一天")) and any(
        marker in compact for marker in ("日报", "日志", "复盘", "草稿", "今日工作", "问题", "风险", "明日计划", "计划")
    )


def _label_score(compact_input: str, labels: tuple[str, ...]) -> int:
    score = 0
    for label in labels:
        if label and label in compact_input:
            score = max(score, len(label))
    return score


def _compact(value: str) -> str:
    return "".join(str(value or "").split()).strip()
