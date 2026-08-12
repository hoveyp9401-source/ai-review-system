from __future__ import annotations

import hashlib
import json

from app.agent2.memory import TrustedPersonalMemoryContext
from app.agent2.tool_calling.contracts import ToolReceipt
from app.agent2.tool_calling.runtime import TurnExecutionPlan
from app.agent2.tool_calling.write_reply import validate_write_reply


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
    """Return model wording only after its execution claims match server receipts."""

    model_hash = hashlib.sha256(model_content.encode("utf-8")).hexdigest()
    if not write_batch_seen:
        return model_content, model_hash
    envelope, errors = validate_write_reply(model_content, receipts)
    if envelope is None:
        raise ValueError(
            "write reply failed receipt validation: " + "; ".join(errors)
        )
    del personal_memory
    return envelope.reply, model_hash


def _render_report_snapshot(
    snapshot: dict,
    *,
    show_item_numbers: bool = True,
) -> str:
    report_date = str(snapshot.get("report_date") or "")
    fields = snapshot.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    acknowledged_empty_fields = {
        str(field_name)
        for field_name in snapshot.get("acknowledged_empty_fields", ())
        if str(field_name)
    }
    sections = (
        ("today_work", "今日工作", fields.get("today_work")),
        ("problems", "问题风险", fields.get("problems")),
        ("tomorrow_plan", "明日计划", fields.get("tomorrow_plan")),
    )
    lines = [f"{report_date} 日报"]
    for field_name, title, raw_items in sections:
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
            lines.append(
                "暂无明显问题"
                if field_name == "problems"
                and field_name in acknowledged_empty_fields
                else (
                    "暂无"
                    if field_name in acknowledged_empty_fields
                    else "（未填写）"
                )
            )
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
        return (
            "这次操作没有执行，日报内容和状态都没有变化。"
            "请再说一次要增加、修改、删除或移动的具体内容。"
        )
    if reason == "tool_call_canary_report_incomplete":
        return (
            "这份日报还没填完整，请补充今日工作、问题/风险和明日计划中的缺项；"
            "如果某一栏确实没有内容，直接自然说明即可。本次没有提交。"
        )
    return "这条消息暂时没有处理成功，本次没有写入任何内容。"
