from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent2.context_pack import Agent2ContextPack, KnowledgeEvidenceFrame
from app.agent2.fact_contracts import build_case_table_fact_contract


@dataclass(frozen=True)
class RagQaReply:
    text: str
    source: str = "rag_qa"


def build_rag_qa_reply(
    *,
    raw_text: str,
    context_pack: Agent2ContextPack | None,
    reply_type: str = "",
) -> RagQaReply | None:
    """Render read-only answers from structured RAG evidence.

    The module is intentionally deterministic: facts come from adapters, this
    layer only chooses a compact user-facing shape. LLMs may still be used as a
    fallback, but they should not be responsible for counting or filtering.
    """

    if context_pack is None:
        return None
    evidence = _first_case_table_structured_evidence(context_pack)
    if evidence is None:
        return None
    facts = evidence.facts
    if not isinstance(facts, dict):
        return None

    if facts.get("permission_denied"):
        body = _permission_denied_reply(facts)
    elif facts.get("metric_mode") == "defendant_monthly":
        body = _defendant_monthly_reply(facts)
    elif facts.get("group_by") == "department":
        body = _department_group_reply(facts)
    elif "case_count" in facts:
        body = _case_scope_reply(facts)
    else:
        return None
    if not body:
        return None

    prefix = "\u8fd9\u53e5\u6211\u6309\u3010\u5185\u90e8\u95ee\u7b54\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002"
    return RagQaReply(text=f"{prefix}\n{body}")


def _first_case_table_structured_evidence(context_pack: Agent2ContextPack) -> KnowledgeEvidenceFrame | None:
    for evidence in getattr(context_pack, "knowledge", ()) or ():
        if getattr(evidence, "source_type", "") != "case_table_rag":
            continue
        facts = getattr(evidence, "facts", None)
        if not isinstance(facts, dict):
            continue
        _ensure_fact_contract(evidence, facts)
        if (
            facts.get("permission_denied")
            or facts.get("metric_mode") == "defendant_monthly"
            or facts.get("group_by") == "department"
            or "case_count" in facts
        ):
            return evidence
    return None


def _ensure_fact_contract(evidence: KnowledgeEvidenceFrame, facts: dict[str, Any]) -> None:
    if facts.get("permission_denied"):
        return
    if isinstance(facts.get("fact_contract"), dict):
        return
    if not _looks_like_case_table_facts(facts):
        return
    facts["fact_contract"] = build_case_table_fact_contract(
        source_id=getattr(evidence, "source_id", "") or "case_table_rag",
        title=getattr(evidence, "title", "") or "case table fact",
        facts=facts,
        confidence=float(getattr(evidence, "confidence", 0.0) or 0.0),
        freshness=str(getattr(evidence, "freshness", "") or ""),
    )


def _looks_like_case_table_facts(facts: dict[str, Any]) -> bool:
    return (
        bool(facts.get("permission_denied"))
        or "case_count" in facts
        or "groups" in facts
        or facts.get("metric_mode") == "defendant_monthly"
        or bool(facts.get("case_name"))
    )


def _permission_denied_reply(facts: dict[str, Any]) -> str:
    permission = facts.get("permission") if isinstance(facts.get("permission"), dict) else {}
    reason = str(permission.get("reason") or "当前问题超出你的可查询范围").strip()
    target = permission.get("target_scope") if isinstance(permission.get("target_scope"), dict) else {}
    scope_type = str(target.get("scope_type") or "").strip()
    if scope_type in {"all", "all_teams"}:
        scope_text = "全部团队或跨团队数据"
    elif target.get("department"):
        scope_text = str(target.get("department"))
    elif target.get("assignee_name"):
        scope_text = str(target.get("assignee_name"))
    else:
        scope_text = "该范围"
    return f"这类数据我需要先做权限校验。当前你不能查询【{scope_text}】的案件底表统计；{reason}。"


def _defendant_monthly_reply(facts: dict[str, Any]) -> str:
    if facts.get("group_by") == "department":
        return _defendant_monthly_group_reply(facts)
    metric_kind = str(facts.get("metric_kind") or "inventory")
    scope = _scope_label(facts)
    as_of = str(facts.get("as_of_date") or "").strip()
    period_label = str(facts.get("period_label") or "").strip()
    period_type = str(facts.get("period_type") or "month")
    period_unit = "\u81ea\u7136\u5b63\u5ea6" if period_type == "quarter" else "\u81ea\u7136\u6708"
    inventory = _int_value(facts.get("inventory_count"))
    new_count = _int_value(facts.get("new_count"))
    lines = [f"\u6839\u636e\u88ab\u544a\u6848\u4ef6\u5e95\u8868\uff0c{scope}\u7684\u7edf\u8ba1\u53e3\u5f84\u5982\u4e0b\uff1a"]
    if metric_kind in {"inventory", "inventory_and_new"}:
        lines.append(f"- \u5b58\u91cf\uff1a\u622a\u81f3 {as_of}\uff0c\u5171 {inventory} \u4ef6\u3002")
        lines.extend(_change_lines(facts, prefix="\u5b58\u91cf", yoy_key="inventory_yoy_change", mom_key="inventory_mom_change"))
    if metric_kind in {"new", "inventory_and_new"}:
        lines.append(f"- \u65b0\u589e\uff1a{period_label} {period_unit}\u5185\u65b0\u589e {new_count} \u4ef6\u3002")
        lines.extend(_change_lines(facts, prefix="\u65b0\u589e", yoy_key="new_yoy_change", mom_key="new_mom_change"))
    names = _sample_case_names(facts)
    display_count = inventory if metric_kind != "new" else new_count
    if 0 < display_count <= 10 and names:
        lines.append("\u5177\u4f53\u6848\u4ef6\uff1a")
        for index, name in enumerate(names[:10], start=1):
            lines.append(f"{index}. {name}")
    elif names:
        lines.append("\u6837\u4f8b\uff1a" + "\uff1b".join(names[:3]) + "\u3002")
    lines.append(
        "\u53e3\u5f84\uff1a\u5b58\u91cf=\u767b\u8bb0\u65e5\u2264\u622a\u6b62\u65e5\uff0c\u4e14\uff08\u672a\u7ed3\u6848\u6216\u7ed3\u6848\u65e5>\u622a\u6b62\u65e5\uff09\uff1b"
        f"\u65b0\u589e=\u767b\u8bb0\u65e5\u5728\u7edf\u8ba1{period_unit}\u5185\u3002"
    )
    return "\n".join(lines)


def _defendant_monthly_group_reply(facts: dict[str, Any]) -> str:
    groups = facts.get("groups") or ()
    if not isinstance(groups, (list, tuple)):
        return ""
    metric_kind = str(facts.get("metric_kind") or "inventory")
    as_of = str(facts.get("as_of_date") or "").strip()
    period_label = str(facts.get("period_label") or "").strip()
    period_type = str(facts.get("period_type") or "month")
    period_unit = "\u81ea\u7136\u5b63\u5ea6" if period_type == "quarter" else "\u81ea\u7136\u6708"
    label = "\u65b0\u589e" if metric_kind == "new" else "\u5b58\u91cf"
    when = period_label if metric_kind == "new" else f"\u622a\u81f3 {as_of}"
    lines = [f"\u6839\u636e\u88ab\u544a\u6848\u4ef6\u5e95\u8868\uff0c\u5404\u56e2\u961f\u88ab\u544a\u6848\u4ef6{label}\uff08{when}\uff09\u5982\u4e0b\uff1a"]
    for index, item in enumerate(list(groups)[:12], start=1):
        if not isinstance(item, dict):
            continue
        department = str(item.get("department") or "\u672a\u6807\u6ce8\u90e8\u95e8").strip()
        count = _int_value(item.get("case_count"))
        extra = ""
        if metric_kind != "new":
            extra = _compact_change_suffix(item, "inventory_mom_change")
        else:
            extra = _compact_change_suffix(item, "new_mom_change")
        lines.append(f"{index}. {department}\uff1a{count} \u4ef6{extra}")
    if len(groups) > 12:
        lines.append(f"\u5176\u4f59 {len(groups) - 12} \u4e2a\u56e2\u961f\u672a\u5c55\u5f00\uff0c\u53ef\u4ee5\u7ee7\u7eed\u95ee\u5177\u4f53\u56e2\u961f\u3002")
    lines.append(
        "\u53e3\u5f84\uff1a\u5b58\u91cf=\u767b\u8bb0\u65e5\u2264\u622a\u6b62\u65e5\uff0c\u4e14\uff08\u672a\u7ed3\u6848\u6216\u7ed3\u6848\u65e5>\u622a\u6b62\u65e5\uff09\uff1b"
        f"\u65b0\u589e=\u767b\u8bb0\u65e5\u5728\u7edf\u8ba1{period_unit}\u5185\u3002"
    )
    return "\n".join(lines)


def _change_lines(facts: dict[str, Any], *, prefix: str, yoy_key: str, mom_key: str) -> list[str]:
    lines: list[str] = []
    yoy = _float_or_none(facts.get(yoy_key))
    mom = _float_or_none(facts.get(mom_key))
    if yoy is not None:
        lines.append(f"  \u540c\u6bd4\uff1a{prefix}{_change_phrase(yoy)}\u3002")
    if mom is not None:
        lines.append(f"  \u73af\u6bd4\uff1a{prefix}{_change_phrase(mom)}\u3002")
    return lines


def _compact_change_suffix(facts: dict[str, Any], key: str) -> str:
    value = _float_or_none(facts.get(key))
    if value is None:
        return ""
    return f"\uff0c\u73af\u6bd4{_change_phrase(value)}"


def _change_phrase(value: float) -> str:
    if value < 0:
        return f"\u4e0b\u964d {abs(value):.2f}%"
    if value > 0:
        return f"\u589e\u957f {value:.2f}%"
    return "\u6301\u5e73"


def _department_group_reply(facts: dict[str, Any]) -> str:
    groups = facts.get("groups") or ()
    if not isinstance(groups, (list, tuple)):
        return ""
    table_label = _table_label(facts)
    status_label = _status_label(facts)
    total = _int_value(facts.get("case_count"))
    lines = [f"\u6839\u636e\u6848\u4ef6\u5e95\u8868\uff0c{table_label}{status_label}\u6848\u4ef6\u6309\u56e2\u961f\u7edf\u8ba1\u5171 {total} \u4ef6\uff1a"]
    visible = list(groups)[:12]
    for index, item in enumerate(visible, start=1):
        if not isinstance(item, dict):
            continue
        department = str(item.get("department") or "\u672a\u6807\u6ce8\u90e8\u95e8").strip()
        count = _int_value(item.get("case_count"))
        lines.append(f"{index}. {department}\uff1a{count} \u4ef6")
    if len(groups) > len(visible):
        lines.append(f"\u5176\u4f59 {len(groups) - len(visible)} \u4e2a\u56e2\u961f\u672a\u5c55\u5f00\uff0c\u53ef\u4ee5\u7ee7\u7eed\u95ee\u5177\u4f53\u56e2\u961f\u3002")
    lines.append("\u53e3\u5f84\uff1a\u6765\u81ea\u6848\u4ef6\u5e95\u8868\u7d22\u5f15\u3002")
    return "\n".join(lines)


def _case_scope_reply(facts: dict[str, Any]) -> str:
    table_label = _table_label(facts)
    status_label = _status_label(facts)
    scope = _scope_label(facts)
    count = _int_value(facts.get("case_count"))
    total = _int_value(facts.get("total_case_count"))
    query_mode = str(facts.get("query_mode") or "count")
    names = _sample_case_names(facts)

    if status_label and total and total != count:
        first = f"\u6839\u636e\u6848\u4ef6\u5e95\u8868\uff0c{scope}{table_label}\u6848\u4ef6\u4e2d{status_label}\u7684\u6709 {count} \u4ef6\uff08\u8be5\u8303\u56f4\u603b\u8ba1 {total} \u4ef6\uff09\u3002"
    else:
        first = f"\u6839\u636e\u6848\u4ef6\u5e95\u8868\uff0c{scope}{table_label}{status_label}\u6848\u4ef6\u5171 {count} \u4ef6\u3002"
    lines = [first]

    should_list = query_mode == "list" or (0 < count <= 10)
    if should_list and names:
        lines.append("\u5177\u4f53\u6848\u4ef6\uff1a")
        for index, name in enumerate(names, start=1):
            lines.append(f"{index}. {name}")
        if bool(facts.get("sample_truncated")):
            lines.append(f"\u4ee5\u4e0a\u5148\u5217\u524d {len(names)} \u4ef6\uff0c\u9700\u8981\u53ef\u4ee5\u7ee7\u7eed\u8ffd\u95ee\u66f4\u591a\u660e\u7ec6\u3002")
    elif names:
        lines.append("\u6837\u4f8b\uff1a" + "\uff1b".join(names[:3]) + "\u3002")
    lines.append("\u53e3\u5f84\uff1a\u6765\u81ea\u6848\u4ef6\u5e95\u8868\u7d22\u5f15\u3002")
    return "\n".join(lines)


def _scope_label(facts: dict[str, Any]) -> str:
    department = str(facts.get("department") or "").strip()
    assignee = str(facts.get("assignee_name") or "").strip()
    if department:
        return department
    if assignee:
        return assignee
    return "\u5168\u90e8"


def _table_label(facts: dict[str, Any]) -> str:
    label = str(facts.get("table_label") or "").strip()
    return label or "\u6848\u4ef6"


def _status_label(facts: dict[str, Any]) -> str:
    status_filter = str(facts.get("status_filter") or "").strip()
    if status_filter == "unclosed" or bool(facts.get("unclosed_only")):
        return "\u672a\u7ed3\u6848"
    if status_filter == "closed":
        return "\u5df2\u7ed3\u6848"
    return ""


def _sample_case_names(facts: dict[str, Any]) -> list[str]:
    raw = facts.get("sample_case_names") or ()
    if not isinstance(raw, (list, tuple)):
        return []
    names: list[str] = []
    for value in raw:
        name = str(value or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
