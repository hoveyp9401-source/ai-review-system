from __future__ import annotations

import re
from collections.abc import Iterable

from app.agent2.tool_calling.contracts import ReceiptStatus, ToolReceipt


_GROUPS = (
    ("已完成", "completed_members"),
    ("部分填写", "partial_members"),
    ("未填写", "not_filled_members"),
)
_INTERNAL_TERMS = (
    "team_ref",
    "member_ref",
    "responsibility_unknown",
    "confirmed_missing_members",
    "confirmed missing",
    "not_yet_due_members",
)


def validate_managed_daily_reply(
    content: str,
    receipts: tuple[ToolReceipt, ...],
) -> tuple[str, ...]:
    """Validate facts and layout without composing a business reply."""

    query = _missing_submission_query(receipts)
    if query is None:
        return ()
    text = str(content or "").strip()
    compact = re.sub(r"\s+", "", text)
    errors: list[str] = []
    if not text:
        errors.append("empty_reply")
        return tuple(errors)

    report_date = str(query.get("report_date") or "").strip()
    if report_date and not _contains_date(text, report_date):
        errors.append("missing_exact_report_date")
    scope_name = str(query.get("scope_name") or "").strip()
    if scope_name and not any(
        label in text for label in _accepted_scope_labels(scope_name)
    ):
        errors.append("missing_scope_name")

    for label, key in _GROUPS:
        members = _named_members(query.get(key))
        if label not in text:
            errors.append(f"missing_group:{label}")
        elif not re.search(
            rf"{re.escape(label)}[^\d]{{0,8}}{len(members)}人",
            compact,
        ):
            errors.append(f"wrong_or_missing_count:{label}")
        missing_names = [name for name in members if name not in text]
        if missing_names:
            errors.append(
                f"missing_members:{label}:" + "、".join(missing_names)
            )

    leaked = [term for term in _INTERNAL_TERMS if term in text]
    if leaked:
        errors.append("internal_terms:" + ",".join(leaked))
    if "| ---" in text or "|---" in compact:
        errors.append("markdown_table")
    return tuple(errors)


def managed_daily_reply_retry_instruction(
    errors: Iterable[str],
) -> str:
    return (
        "工具查询阶段已经结束，禁止再次调用任何工具，也禁止输出XML、DSML或"
        "工具调用标记。请只输出最终中文纯文本答复。服务器校验发现上一版答复"
        "存在这些问题："
        + "; ".join(errors)
        + "。请重新读取对话里已经返回的工具事实，按Managed daily-report reply "
        "contract重写最终答复。不要提及本次校验，不要让服务器代写业务答案。"
    )


def _accepted_scope_labels(scope_name: str) -> tuple[str, ...]:
    labels = [scope_name]
    normalized = scope_name.replace("／", "/")
    if "/" in normalized:
        leaf = normalized.rsplit("/", 1)[-1].strip()
        if leaf and leaf not in labels:
            labels.append(leaf)
    return tuple(labels)


def _missing_submission_query(
    receipts: tuple[ToolReceipt, ...],
) -> dict[str, object] | None:
    for receipt in reversed(receipts):
        if receipt.status not in {
            ReceiptStatus.SUCCESS,
            ReceiptStatus.NO_OP,
        }:
            continue
        query = receipt.safe_user_facts.get("managed_daily_query")
        if (
            isinstance(query, dict)
            and query.get("query_kind") == "missing_submissions"
        ):
            return query
    return None


def _named_members(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        name
        for item in value
        if isinstance(item, dict)
        and (name := str(item.get("name") or "").strip())
    )


def _contains_date(text: str, iso_date: str) -> bool:
    if iso_date in text:
        return True
    try:
        year_text, month_text, day_text = iso_date.split("-", 2)
        month = str(int(month_text))
        day = str(int(day_text))
    except (ValueError, TypeError):
        return False
    return (
        f"{year_text}年{month}月{day}日" in text
        or f"{month}月{day}日" in text
    )
