from __future__ import annotations

import hashlib
import json

from app.agent2.memory import (
    TogglePreferenceValue,
    TrustedPersonalMemoryContext,
)
from app.agent2.tool_calling.contracts import ReceiptStatus, ToolReceipt
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.runtime import TurnExecutionPlan


def finalize_shadow_content(
    model_content: str,
    plan: TurnExecutionPlan | None,
) -> tuple[str, str]:
    model_hash = hashlib.sha256(model_content.encode("utf-8")).hexdigest()
    if plan is None:
        return model_content, model_hash
    safe_facts = [
        receipt.safe_user_facts
        for receipt in plan.receipts
    ]
    return (
        json.dumps(
            {"safe_user_facts": safe_facts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        model_hash,
    )


def finalize_canary_content(
    model_content: str,
    receipts: tuple[ToolReceipt, ...],
    *,
    write_batch_seen: bool,
    personal_memory: TrustedPersonalMemoryContext | None = None,
) -> tuple[str, str]:
    """Use deterministic Receipt facts for writes; never trust model success prose."""

    model_hash = hashlib.sha256(model_content.encode("utf-8")).hexdigest()
    managed_missing_reply = _managed_missing_submission_reply(
        receipts
    )
    if managed_missing_reply is not None:
        return managed_missing_reply, model_hash
    if not write_batch_seen:
        return model_content, model_hash
    failed = next(
        (
            receipt
            for receipt in receipts
            if receipt.status
            in {
                ReceiptStatus.BLOCKED,
                ReceiptStatus.CLARIFICATION_REQUIRED,
                ReceiptStatus.FAILED,
            }
        ),
        None,
    )
    if failed is not None:
        code = failed.error_code or failed.status.value
        if code == "HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF":
            report_date = str(
                failed.safe_user_facts.get("report_date")
                or "该日期"
            )
            locked_after = str(
                failed.safe_user_facts.get("locked_after")
                or "09:00"
            )
            return (
                f"{report_date} 的日报已在次日 {locked_after} 后限制修改。"
                "现在仍可补充新增条目，但不能修改、删除、移动或清空已有内容。"
            ), model_hash
        return f"本次未执行，服务端回执：{code}。请补充信息或稍后重试。", model_hash

    write_receipts = tuple(
        receipt
        for receipt in receipts
        if (
            (definition := TOOL_REGISTRY.get(receipt.tool_name))
            is not None
            and definition.read_or_write == "write"
        )
    )
    memory_mutations = tuple(
        receipt
        for receipt in write_receipts
        if receipt.target_type == "personal_memory"
        and receipt.safe_user_facts.get("memory_key")
    )
    memory_reply = (
        _personal_memory_write_reply(memory_mutations)
        if memory_mutations
        else ""
    )
    request_clear = next(
        (
            receipt
            for receipt in receipts
            if receipt.safe_user_facts.get("confirmation_required") is True
        ),
        None,
    )
    if request_clear is not None:
        target_date = request_clear.safe_user_facts.get("target_date") or ""
        return (
            _combine_write_replies(
                f"已为{target_date or '该日期'}的日报创建清空确认，"
                "请在下一条消息中确认清空。",
                memory_reply,
            ),
            model_hash,
        )
    confirm_clear = next(
        (
            receipt
            for receipt in receipts
            if receipt.safe_user_facts.get("pending_consumed") is True
        ),
        None,
    )
    if confirm_clear is not None:
        target_date = confirm_clear.safe_user_facts.get("report_date") or ""
        return (
            _combine_write_replies(
                f"{target_date or '目标日期'}的日报已清空。",
                memory_reply,
            ),
            model_hash,
        )

    if memory_mutations and all(
        receipt.target_type == "personal_memory"
        for receipt in write_receipts
    ):
        return _personal_memory_write_reply(memory_mutations), model_hash

    daily_write_receipts = tuple(
        receipt
        for receipt in write_receipts
        if receipt.target_type == "daily_report"
    )
    changed = tuple(
        receipt for receipt in daily_write_receipts if receipt.changed
    )
    show_updated_snapshot = _trusted_toggle_preference(
        personal_memory,
        memory_key="report.show_updated_snapshot",
        default=True,
    )
    show_item_numbers = _trusted_toggle_preference(
        personal_memory,
        memory_key="report.show_item_numbers",
        default=True,
    )
    report_snapshots = (
        _latest_report_snapshots(daily_write_receipts)
        if show_updated_snapshot
        else ()
    )
    if not changed:
        if report_snapshots:
            return (
                _combine_write_replies(
                    "已核对，没有产生重复变更。\n\n"
                    + _render_report_snapshots(
                        report_snapshots,
                        show_item_numbers=show_item_numbers,
                    ),
                    memory_reply,
                ),
                model_hash,
            )
        report_date = next(
            (
                str(receipt.safe_user_facts.get("report_date") or "")
                for receipt in daily_write_receipts
                if receipt.safe_user_facts.get("report_date")
            ),
            "",
        )
        return (
            _combine_write_replies(
                f"{report_date or '本次请求'}已核对，没有产生重复变更。",
                memory_reply,
            ),
            model_hash,
        )
    if report_snapshots:
        return (
            _combine_write_replies(
                "已更新，当前日报如下：\n\n"
                + _render_report_snapshots(
                    report_snapshots,
                    show_item_numbers=show_item_numbers,
                ),
                memory_reply,
            ),
            model_hash,
        )
    dates = sorted(
        {
            str(receipt.safe_user_facts.get("report_date"))
            for receipt in changed
            if receipt.safe_user_facts.get("report_date")
        }
    )
    affected_count = len(
        {
            item_id
            for receipt in changed
            for item_id in receipt.affected_item_ids
        }
    )
    date_text = "、".join(dates) if dates else "目标日期"
    return (
        _combine_write_replies(
            f"日报已更新：{date_text}，共调整{affected_count}条内容。",
            memory_reply,
        ),
        model_hash,
    )


def _managed_missing_submission_reply(
    receipts: tuple[ToolReceipt, ...],
) -> str | None:
    query = next(
        (
            facts
            for receipt in reversed(receipts)
            if receipt.status
            in {ReceiptStatus.SUCCESS, ReceiptStatus.NO_OP}
            and isinstance(
                (
                    facts := receipt.safe_user_facts.get(
                        "managed_daily_query"
                    )
                ),
                dict,
            )
            and facts.get("query_kind") == "missing_submissions"
        ),
        None,
    )
    if query is None:
        return None
    completed = _safe_named_members(
        query.get("completed_members")
    )
    partial = _safe_named_members(
        query.get("partial_members")
    )
    not_filled_value = query.get("not_filled_members")
    if isinstance(not_filled_value, list):
        not_filled = _safe_named_members(not_filled_value)
    else:
        not_filled = _unique_named_members(
            _safe_named_members(
                query.get("confirmed_missing_members")
            ),
            _safe_named_members(query.get("not_yet_due_members")),
        )
    report_date = str(query.get("report_date") or "目标日期")
    scope_name = str(query.get("scope_name") or "当前范围")
    lines = [f"{report_date}（{scope_name}）日报填写情况："]
    lines.extend(_managed_status_lines("已完成", completed))
    lines.extend(_managed_status_lines("部分填写", partial))
    lines.extend(_managed_status_lines("未填写", not_filled))
    return "\n".join(lines)


def _safe_named_members(
    value: object,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        return ()
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            result.append(
                (
                    name,
                    str(item.get("team_name") or "").strip(),
                )
            )
    return tuple(result)


def _unique_named_members(
    *groups: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    unique: dict[tuple[str, str], None] = {}
    for group in groups:
        for item in group:
            unique.setdefault(item, None)
    return tuple(unique)


def _managed_status_lines(
    label: str,
    members: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    if not members:
        return (f"{label} 0 人",)
    by_team: dict[str, list[str]] = {}
    for name, team_name in members:
        by_team.setdefault(team_name, []).append(name)
    if len(by_team) == 1:
        names = next(iter(by_team.values()))
        return (f"{label} {len(members)} 人：{'、'.join(names)}",)
    return (
        f"{label} {len(members)} 人：",
        *(
            f"- {team_name or '未分组'}：{'、'.join(names)}"
            for team_name, names in by_team.items()
        ),
    )


def _combine_write_replies(
    daily_reply: str,
    memory_reply: str,
) -> str:
    if not memory_reply:
        return daily_reply
    return f"{daily_reply}\n\n{memory_reply}"


def _latest_report_snapshots(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[dict, ...]:
    by_date: dict[str, dict] = {}
    for receipt in receipts:
        snapshot = receipt.safe_user_facts.get("report_snapshot")
        if not isinstance(snapshot, dict):
            continue
        report_date = str(snapshot.get("report_date") or "").strip()
        fields = snapshot.get("fields")
        if not report_date or not isinstance(fields, dict):
            continue
        by_date[report_date] = snapshot
    return tuple(by_date[key] for key in sorted(by_date))


def _personal_memory_write_reply(
    receipts: tuple[ToolReceipt, ...],
) -> str:
    changed = tuple(receipt for receipt in receipts if receipt.changed)
    if not changed:
        if any(
            isinstance(receipt.safe_user_facts.get("memory"), dict)
            for receipt in receipts
        ):
            return "这项偏好已是当前设置，无需重复保存。"
        return "这项偏好当前没有保存，无需变更。"
    if all(
        receipt.safe_user_facts.get("forgotten") is True
        for receipt in changed
    ):
        return "已清除这项偏好。"
    if all(
        isinstance(receipt.safe_user_facts.get("memory"), dict)
        for receipt in changed
    ):
        return "已记住这项偏好。"
    return "已更新个人偏好。"


def _trusted_toggle_preference(
    personal_memory: TrustedPersonalMemoryContext | None,
    *,
    memory_key: str,
    default: bool,
) -> bool:
    if personal_memory is None:
        return default
    for entry in personal_memory.entries:
        if entry.memory_key != memory_key:
            continue
        if isinstance(entry.value, TogglePreferenceValue):
            return entry.value.enabled
        return default
    return default


def _render_report_snapshots(
    snapshots: tuple[dict, ...],
    *,
    show_item_numbers: bool = True,
) -> str:
    return "\n\n".join(
        _render_report_snapshot(
            item,
            show_item_numbers=show_item_numbers,
        )
        for item in snapshots
    )


def _render_report_snapshot(
    snapshot: dict,
    *,
    show_item_numbers: bool = True,
) -> str:
    report_date = str(snapshot.get("report_date") or "")
    fields = snapshot.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    sections = (
        ("今日工作", fields.get("today_work")),
        ("问题风险", fields.get("problems")),
        ("明日计划", fields.get("tomorrow_plan")),
    )
    lines = [f"{report_date} 日报"]
    for title, raw_items in sections:
        items = (
            [str(item).strip() for item in raw_items if str(item).strip()]
            if isinstance(raw_items, list)
            else []
        )
        lines.extend(("", title))
        if items:
            if show_item_numbers:
                lines.extend(
                    f"{index}. {content}"
                    for index, content in enumerate(items, start=1)
                )
            else:
                lines.extend(f"- {content}" for content in items)
        else:
            lines.append("（未填写）")
    lines.extend(
        (
            "",
            f"状态：{_user_status(str(snapshot.get('status') or ''))}",
        )
    )
    return "\n".join(lines)


def _user_status(status: str) -> str:
    return {
        "collecting": "收集整理中",
        "pending_confirmation": "待确认",
        "completed": "已提交",
        "skipped": "已跳过",
        "cancelled": "已取消",
    }.get(status, "处理中")


def canary_block_message(reason: str) -> str:
    if reason == "tool_call_canary_server_scope_missing":
        return "当前账号信息无法确认，本次没有执行任何操作。"
    if reason == "tool_call_canary_execution_failed":
        return "刚才没有处理完整，本次没有写入任何内容。请再发一次。"
    if reason == "tool_call_canary_report_already_submitted":
        return "这份日报已经提交，当前不能继续追加内容。本次没有写入。"
    return "这条消息暂时没有处理成功，本次没有写入任何内容。"
