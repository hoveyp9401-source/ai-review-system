from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.agent2.context_pack import Agent2ContextPack, KnowledgeEvidenceFrame
from app.utils.json import extract_json_object

logger = logging.getLogger(__name__)


SUBSTANTIAL_LOSS_AMOUNT_DEFINITION = (
    "实质减损金额按年初至统计截止日累计：仅纳入已结案，且对方单位性质为"
    "供应商或班组、案情原因为“无争议-债权债务明确”、减损金额大于0的案件，"
    "并汇总这些案件的减损金额。"
)


@dataclass(frozen=True)
class PerformanceQaReply:
    text: str
    source: str = "performance_report"


async def build_performance_qa_reply(
    *,
    raw_text: str,
    context_pack: Agent2ContextPack | None,
    llm_client: Any,
) -> PerformanceQaReply | None:
    """Select a permitted report with the model, then render its fixed facts.

    The model receives only the question and the allowed scope names. It never
    receives source rows, formulas, or permission to calculate a metric.
    """

    evidence = _performance_catalog(context_pack)
    if evidence is None:
        return None
    facts = evidence.facts if isinstance(evidence.facts, dict) else {}
    reports = facts.get("reports")
    if not isinstance(reports, dict):
        return None
    allowed_scopes = _allowed_scopes(reports)
    if not allowed_scopes:
        return None

    selection = await _select_report(
        raw_text=raw_text,
        allowed_scopes=allowed_scopes,
        llm_client=llm_client,
    )
    if selection is None or not selection.get("matched"):
        return None
    view = str(selection.get("view") or "")
    scope_key = str(selection.get("scope_key") or "")
    if view not in {"week", "month"}:
        return None
    allowed_keys = {item["scope_key"] for item in allowed_scopes}
    if scope_key not in allowed_keys:
        return PerformanceQaReply(
            text=(
                "当前可查询范围不包含这个团队；我没有展示或推算该团队的绩效数据。"
            ),
            source="performance_report_permission",
        )
    report = reports.get(view)
    if not isinstance(report, dict):
        return None
    scope = next(
        (
            item
            for item in report.get("scopes") or ()
            if isinstance(item, dict) and str(item.get("scope_key") or "") == scope_key
        ),
        None,
    )
    if scope is None:
        return None
    return PerformanceQaReply(
        text=render_performance_report(
            report=report,
            scope=scope,
            source_status=str(facts.get("source_status_label") or "当前可用底表"),
            rule_version=str(facts.get("rule_version") or ""),
            mode=str(selection.get("mode") or "summary"),
        )
    )


def render_performance_report(
    *,
    report: dict[str, Any],
    scope: dict[str, Any],
    source_status: str,
    rule_version: str,
    mode: str = "summary",
) -> str:
    """Render server-calculated facts without asking the model to calculate."""

    return _render_report(
        report=report,
        scope=scope,
        source_status=source_status,
        rule_version=rule_version,
        mode=mode,
    )


def _performance_catalog(
    context_pack: Agent2ContextPack | None,
) -> KnowledgeEvidenceFrame | None:
    if context_pack is None:
        return None
    for evidence in getattr(context_pack, "knowledge", ()) or ():
        if (
            str(getattr(evidence, "source_type", "") or "")
            != "performance_report_catalog"
        ):
            continue
        facts = getattr(evidence, "facts", None)
        if isinstance(facts, dict) and isinstance(facts.get("reports"), dict):
            return evidence
    return None


def _allowed_scopes(reports: dict[str, Any]) -> list[dict[str, str]]:
    seen: set[str] = set()
    scopes: list[dict[str, str]] = []
    for view in ("week", "month"):
        report = reports.get(view)
        if not isinstance(report, dict):
            continue
        for item in report.get("scopes") or ():
            if not isinstance(item, dict):
                continue
            key = str(item.get("scope_key") or "").strip()
            name = str(item.get("scope_name") or "").strip()
            scope_type = str(item.get("scope_type") or "team").strip()
            if not key or not name or key in seen:
                continue
            seen.add(key)
            scopes.append(
                {
                    "scope_key": key,
                    "scope_name": name,
                    "scope_type": scope_type,
                }
            )
    return scopes


async def _select_report(
    *,
    raw_text: str,
    allowed_scopes: list[dict[str, str]],
    llm_client: Any,
) -> dict[str, Any] | None:
    settings = getattr(llm_client, "settings", None)
    try:
        output = await llm_client.complete_json(
            system_prompt=(
                "你只负责选择被告案件绩效报表，不计算任何指标。"
                "严格返回JSON："
                '{"matched":true|false,"view":"week|month|",'
                '"scope_key":"允许值或空字符串",'
                '"mode":"summary|explain_new|explain_stock|explain_loss|'
                'explain_substantial"}。'
                "仅当用户确实在问被告绩效、指标完成情况或对应周/月统计时 matched=true。"
                "“本周/周”选择week，“本月/月”选择month；"
                "没有明确周或月时默认选择month；"
                "“部门整体/整体/全部团队”选择整体对应的scope_key。"
                "“我的/我本人/本人”只能选择scope_type为person或"
                "person_unavailable的scope_key。"
                "问“新增怎么算、为什么新增多、新增来自哪里”选择explain_new；"
                "问“为什么存量多、存量来自哪里”选择explain_stock；"
                "问“综合减损率怎么算”选择explain_loss；"
                "问“实质减损金额怎么算”选择explain_substantial；"
                "其他情况选择summary。"
                "团队只能从allowed_scopes中选择；无法确认或超出范围时保留用户想查的意图，"
                "但scope_key返回空字符串。不要输出指标，不要猜测数字。"
            ),
            user_prompt=json.dumps(
                {
                    "question": str(raw_text or "").strip(),
                    "allowed_scopes": allowed_scopes,
                    "available_views": ["week", "month"],
                },
                ensure_ascii=False,
            ),
            model=str(
                getattr(settings, "llm_intent_model", "")
                or getattr(settings, "llm_model", "")
            ),
            thinking_enabled=bool(getattr(settings, "llm_intent_thinking", False)),
            timeout_seconds=float(
                getattr(settings, "llm_intent_timeout_seconds", 8.0) or 8.0
            ),
            max_retries=0,
        )
        payload = extract_json_object(output)
        return payload if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 - selector failure must fall through safely
        logger.info(
            "performance report selector unavailable: %s",
            exc.__class__.__name__,
        )
        return None


def _render_report(
    *,
    report: dict[str, Any],
    scope: dict[str, Any],
    source_status: str,
    rule_version: str,
    mode: str = "summary",
) -> str:
    # Source labels, rule identifiers, and row-level data-maintenance warnings
    # stay in the audited management view. The chat response only presents
    # business results and any omission that changes team attribution.
    del source_status, rule_version
    period = report.get("period") if isinstance(report.get("period"), dict) else {}
    view = str(period.get("view") or "")
    current_label = "本周" if view == "week" else "本月"
    comparison_label = str(
        period.get("comparison_label") or ("上周五" if view == "week" else "上月末")
    )
    scope_name = str(scope.get("scope_name") or "")
    scope_type = str(scope.get("scope_type") or "")
    if scope_type == "person_unavailable":
        return (
            "我按当前已发布的被告绩效规则查询了你的个人范围。\n"
            f"目前“分公司→法务对接人”关系尚未精确匹配到"
            f"你的系统账号姓名“{scope_name}”，因此暂时不能安全计算"
            "你的个人指标，也不会拿团队数据代替。\n"
            "请由绩效维护人员补充你的分公司负责关系后再查询；"
            "如果想看团队结果，请直接说明团队名称。"
        )
    if scope_type == "person":
        scope_label = "你的被告案件绩效"
    else:
        scope_label = "法务部门整体" if scope_name == "整体" else scope_name
    lines = [
        f"{scope_label}｜{period.get('label') or ''}（截至 {period.get('cutoff_date') or ''}）",
        (
            f"- 存量：{_int(scope.get('stock_count'))} 件，"
            f"同比{_rate_display(scope.get('stock_yoy'))}，"
            f"{comparison_label}{_rate_display(scope.get('stock_period_change'))}"
        ),
        (
            f"- 年度累计新增：{_int(scope.get('year_to_date_new_count'))} 件，"
            f"同比{_rate_display(scope.get('new_yoy'))}"
        ),
        (
            f"- {current_label}新增：{_int(scope.get('period_new_count'))} 件；"
            f"{current_label}结案：{_int(scope.get('period_closed_count'))} 件"
        ),
    ]
    loss_metrics = (
        scope.get("loss_metrics")
        if isinstance(scope.get("loss_metrics"), dict)
        else {}
    )
    comprehensive = (
        loss_metrics.get("comprehensive_loss_rate")
        if isinstance(loss_metrics.get("comprehensive_loss_rate"), dict)
        else {}
    )
    substantial = (
        loss_metrics.get("substantial_loss_amount")
        if isinstance(loss_metrics.get("substantial_loss_amount"), dict)
        else {}
    )
    if (
        view == "month"
        and scope_type == "overall"
        and (
            comprehensive.get("status") == "calculated"
            or substantial.get("status") == "calculated"
        )
    ):
        lines.append(
            "- 综合减损率："
            f"{comprehensive.get('display') or '暂不可计算'}；"
            "实质减损金额（年初至截止日）："
            f"{substantial.get('display') or '暂不可计算'}"
        )
    stock_target = (
        scope.get("stock_target") if isinstance(scope.get("stock_target"), dict) else {}
    )
    new_target = (
        scope.get("new_target") if isinstance(scope.get("new_target"), dict) else {}
    )
    if stock_target or new_target:
        target_parts = []
        if stock_target:
            target_parts.append(
                "存量同比目标："
                f"{stock_target.get('status_label') or '目标待确认'}"
                f"{_target_suffix(stock_target)}"
            )
        if new_target:
            target_parts.append(
                "新增同比目标："
                f"{new_target.get('status_label') or '目标待确认'}"
                f"{_target_suffix(new_target)}"
            )
        lines.append("- " + "；".join(target_parts))
    if mode == "explain_new":
        lines.extend(
            _branch_explanation(
                scope,
                count_key="period_new_count",
                current_label=current_label,
                subject_label=(
                    "你负责的分公司" if scope_type == "person" else "该范围内的分公司"
                ),
                metric_label="新增",
            )
        )
        lines.append(
            "- 新增同比口径：今年1月1日至截止日的新增件数，"
            "与去年1月1日至同一截止日的新增件数比较"
        )
    elif mode == "explain_stock":
        lines.extend(
            _branch_explanation(
                scope,
                count_key="stock_count",
                current_label=f"截至{period.get('cutoff_date') or ''}",
                subject_label=(
                    "你负责的分公司" if scope_type == "person" else "该范围内的分公司"
                ),
                metric_label="存量",
            )
        )
        lines.append("- 存量口径：登记日在截止日前，且截止日仍未结案的案件")
    elif mode == "explain_loss":
        lines.extend(
            _loss_explanation(
                comprehensive,
                scope_type=scope_type,
                view=view,
            )
        )
    elif mode == "explain_substantial":
        lines.extend(
            _substantial_explanation(
                substantial,
                scope_type=scope_type,
                view=view,
            )
        )
    assignment_error_count = _int(report.get("assignment_error_count"))
    if assignment_error_count:
        lines.append(
            f"- 数据完整性提醒：有{assignment_error_count}条案件的团队归属"
            "尚未确认，以上数字未包含这些记录，暂不能视为完整结果"
        )
    return "\n".join(lines)


def _rate_display(value: Any) -> str:
    if not isinstance(value, dict):
        return "暂不可比"
    display = str(value.get("display") or "").strip()
    return display or "暂不可比"


def _branch_explanation(
    scope: dict[str, Any],
    *,
    count_key: str,
    current_label: str,
    subject_label: str,
    metric_label: str,
) -> list[str]:
    rows = [
        (
            str(item.get("branch_name") or "").strip(),
            _int(item.get(count_key)),
        )
        for item in scope.get("branches") or ()
        if isinstance(item, dict)
        and str(item.get("branch_name") or "").strip()
        and _int(item.get(count_key)) > 0
    ]
    rows.sort(key=lambda item: (-item[1], item[0]))
    if not rows:
        return [
            (
                f"- {metric_label}构成：{subject_label}{current_label}"
                f"没有{metric_label}记录"
            )
        ]
    total = sum(count for _, count in rows)
    visible = rows[:12]
    detail = "、".join(f"{name} {count}件" for name, count in visible)
    if len(rows) > len(visible):
        hidden_total = sum(count for _, count in rows[len(visible) :])
        detail += f"、其余{len(rows) - len(visible)}个分公司 {hidden_total}件"
    return [
        (
            f"- {metric_label}构成：{subject_label}{current_label}{metric_label}"
            f"共{total}件，分别为：{detail}"
        )
    ]


def _loss_explanation(
    metric: dict[str, Any],
    *,
    scope_type: str,
    view: str,
) -> list[str]:
    if view != "month":
        return [
            "- 当前 Skill 只定义月度减损口径；周维度不计算综合减损率，请查看本月。"
        ]
    if scope_type != "overall":
        return [
            (
                "- 综合减损率按当前 Skill 只计算法务部门整体，不按团队或个人拆分，"
                "因此这里不推算团队值"
            )
        ]
    if not metric or metric.get("status") != "calculated":
        return ["- 综合减损率：当前规则字段或数据不足，暂不可计算"]
    return [
        (
            "- 综合减损率口径：取当前统计期内已结案、且四项应付款至少一项有数据"
            f"的案件，共{_int(metric.get('eligible_case_count'))}件"
        ),
        (
            "- 公式：（标的额及利息合计－应付款合计）÷标的额及利息合计×100%；"
            f"本次标的额及利息合计{_amount_text(metric.get('claim_amount'))}元，"
            f"应付款合计{_amount_text(metric.get('payable_amount'))}元，"
            f"结果为{metric.get('display') or '暂不可计算'}"
        ),
    ]


def _substantial_explanation(
    metric: dict[str, Any],
    *,
    scope_type: str,
    view: str,
) -> list[str]:
    if view != "month":
        return [
            "- 当前 Skill 只定义月度减损口径；周维度不计算实质减损金额，请查看本月。"
        ]
    if scope_type != "overall":
        return [
            (
                "- 实质减损金额按当前 Skill 只计算法务部门整体，不按团队或个人拆分，"
                "因此这里不推算团队值"
            )
        ]
    if not metric or metric.get("status") != "calculated":
        return ["- 实质减损金额：当前规则字段或数据不足，暂不可计算"]
    return [
        f"- 实质减损口径：{SUBSTANTIAL_LOSS_AMOUNT_DEFINITION}",
        (
            f"- 本次符合条件{_int(metric.get('eligible_case_count'))}件，"
            f"减损金额合计{metric.get('display') or '暂不可计算'}"
        ),
    ]


def _amount_text(value: Any) -> str:
    text = str(value or "0").strip()
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _target_suffix(target: dict[str, Any]) -> str:
    display = str(target.get("target_display") or "").strip()
    return f"（目标{display}）" if display else ""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
