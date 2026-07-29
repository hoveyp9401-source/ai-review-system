from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any


REQUIRED_DEPARTMENT_MONTHLY_UNITS = [
    "法务一部",
    "法务二部",
    "法务三部",
    "法务四部",
    "法务五部",
    "法务六部",
    "综合管理部",
    "朱佳佳",
]


LAW_COMMON_METRIC_NAMES = [
    "索赔管理",
    "非诉收款",
    "诉讼案件收款（现金）",
    "诉讼利息收入（现金）",
    "未审定诉讼结算增加额",
    "被告存量/新增案件数量下降率",
]

LAW_COMMON_YEAR_TARGETS = {
    "法务二部": [6000, 20700, 24000, 470, 2710, 10],
    "法务三部": [4400, 12300, 18000, 360, 1500, 10],
    "法务四部": [5800, 17300, 24000, 470, 2000, 10],
    "法务五部": [3000, 9900, 17000, 340, 1600, 10],
    "法务六部": [2700, 8700, 11000, 220, 1100, 10],
}

LAW_FIRST_METRICS = [
    ("诉讼案件收款（现金）", "万元", 27000),
    ("诉讼利息收入（现金）", "万元", 530),
    ("终本案件恢复执行到位率", "%", 20),
    ("未审定诉讼结算增加额", "万元", 2600),
    ("优先权与时效管理", "%", 100),
    ("恒大破产案件申报率", "%", 100),
    ("恒大分配案件清偿率", "%", 20),
    ("恒大衍生风险闭环率", "%", 100),
]

COMPREHENSIVE_METRICS = [
    {"metric_no": 1, "name": "基础综合事务标准化保障", "unit": "", "year_target": ""},
    {"metric_no": 2, "name": "印章管理智能化建设", "unit": "%", "year_target": 100},
    {"metric_no": 3, "name": "AI智能化场景核心转化（完整度/自动化程度）", "unit": "%", "year_target": 100},
    {"metric_no": 4, "name": "AI智能化场景核心转化（数量）", "unit": "分", "year_target": 100},
    {"metric_no": 5, "name": "AI智能化场景核心转化（质量）", "unit": "分", "year_target": 100},
    {"metric_no": 6, "name": "综合服务满意度", "unit": "分", "year_target": 90},
    {"metric_no": 7, "name": "AI场景赋能提效", "unit": "", "year_target": ""},
]

ZHU_JIAJIA_METRICS = [
    {"metric_no": 1, "name": "国别市场研究及合同示范文本", "unit": "项", "year_target": 12},
    {"metric_no": 2, "name": "海外风控及履约风险管理指引", "unit": "项", "year_target": 12},
    {"metric_no": 3, "name": "评审效率", "unit": "%", "year_target": 100},
    {"metric_no": 4, "name": "能力建设", "unit": "项", "year_target": 10},
]


@dataclass(frozen=True)
class DepartmentMonthlyCoverage:
    ready: bool
    expected_units: list[str]
    completed_units: list[str]
    missing_units: list[str]


@dataclass(frozen=True)
class MetricItem:
    department: str
    leader: str
    metric_name: str
    metric_type: str
    unit: str
    month_target: float | None
    month_actual: float | None
    month_completion_rate: float | None
    year_target: float | None
    year_actual_cumulative: float | None
    year_completion_rate: float | None
    yoy_change: float | None
    mom_change: float | None
    unfinished_reason: str
    next_month_target: str
    action_plan: list[str]
    raw_text: str
    metric_no: int | None = None
    status: str = "red"
    validation_flags: tuple[str, ...] = ()
    numerator: float | None = None
    denominator: float | None = None


@dataclass(frozen=True)
class DepartmentMonthlyReport:
    department: str
    leader: str
    period_label: str
    metrics: list[MetricItem]
    source_id: str = ""


@dataclass(frozen=True)
class MetricAggregate:
    metric_name: str
    metric_type: str
    unit: str
    month_target: float | None
    month_actual: float | None
    month_completion_rate: float | None
    year_target: float | None
    year_actual_cumulative: float | None
    year_completion_rate: float | None
    yoy_change: float | None
    mom_change: float | None
    status: str
    note: str = ""


@dataclass(frozen=True)
class LeadershipCoordinationItem:
    matter: str
    involved_departments: str
    affected_metric: str
    current_bottleneck: str
    coordination_action: str
    suggested_deadline: str


@dataclass(frozen=True)
class DepartmentMonthlySummary:
    reports: list[DepartmentMonthlyReport]
    core_metrics: list[MetricAggregate]
    risk_metrics: list[MetricItem]
    leadership_items: list[LeadershipCoordinationItem]
    key_actions: list[str]
    annual_time_progress: float
    non_aggregatable_rate_metrics: list[str]


def template_metrics_for_unit(unit_name: str) -> list[dict[str, Any]]:
    if unit_name == "法务一部":
        return [_metric_template(index, name, unit, year_target) for index, (name, unit, year_target) in enumerate(LAW_FIRST_METRICS, start=1)]
    if unit_name in LAW_COMMON_YEAR_TARGETS:
        values = LAW_COMMON_YEAR_TARGETS[unit_name]
        result = []
        for index, name in enumerate(LAW_COMMON_METRIC_NAMES, start=1):
            unit = "%" if index == 6 else "万元"
            result.append(_metric_template(index, name, unit, values[index - 1]))
        return result
    if unit_name == "综合管理部":
        return [_metric_template(int(item["metric_no"]), str(item["name"]), str(item.get("unit") or ""), item.get("year_target")) for item in COMPREHENSIVE_METRICS]
    if unit_name == "朱佳佳":
        return [_metric_template(int(item["metric_no"]), str(item["name"]), str(item.get("unit") or ""), item.get("year_target")) for item in ZHU_JIAJIA_METRICS]
    return [_metric_template(int(item["metric_no"]), str(item["name"]), str(item["unit"]), item.get("year_target")) for item in ZHU_JIAJIA_METRICS]


def parse_department_monthly_reports(sources: list[dict[str, Any]], *, period_label: str) -> list[DepartmentMonthlyReport]:
    reports: list[DepartmentMonthlyReport] = []
    annual_time_progress = _annual_time_progress(period_label)
    for source in sources:
        if not _source_complete(source):
            continue
        department = _text(source.get("unit_name"))
        leader = _text(source.get("owner_name")) or "负责人"
        metrics = [
            _metric_item_from_source_metric(metric, department=department, leader=leader, annual_time_progress=annual_time_progress)
            for metric in source.get("metrics", [])
            if isinstance(metric, dict)
        ]
        reports.append(
            DepartmentMonthlyReport(
                department=department,
                leader=leader,
                period_label=period_label,
                metrics=metrics,
                source_id=_text(source.get("source_id")),
            )
        )
    return reports


def aggregate_department_monthly_reports(
    reports: list[DepartmentMonthlyReport],
    *,
    period_label: str,
) -> DepartmentMonthlySummary:
    annual_time_progress = _annual_time_progress(period_label)
    metrics = [metric for report in reports for metric in report.metrics]
    core_metrics: list[MetricAggregate] = []
    non_aggregatable_rates: list[str] = []

    grouped: dict[tuple[str, str], list[MetricItem]] = {}
    for metric in metrics:
        if metric.metric_type == "rate" and (metric.numerator is None or metric.denominator is None):
            if metric.metric_name not in non_aggregatable_rates:
                non_aggregatable_rates.append(metric.metric_name)
            continue
        grouped.setdefault((metric.metric_name, metric.metric_type), []).append(metric)

    for (metric_name, metric_type), items in grouped.items():
        aggregate = _aggregate_metric_group(metric_name, metric_type, items, annual_time_progress)
        if aggregate is not None:
            core_metrics.append(aggregate)

    for metric in _risk_metrics(metrics)[:8]:
        if metric.metric_type == "rate" and metric.metric_name in non_aggregatable_rates:
            core_metrics.append(_department_rate_metric_row(metric))

    core_metrics = sorted(core_metrics, key=lambda item: (_status_rank(item.status), item.metric_type, item.metric_name))
    risk_metrics = _risk_metrics(metrics)
    leadership_items = _leadership_coordination_items(risk_metrics)
    key_actions = _key_department_actions(reports, risk_metrics)

    return DepartmentMonthlySummary(
        reports=reports,
        core_metrics=core_metrics,
        risk_metrics=risk_metrics,
        leadership_items=leadership_items,
        key_actions=key_actions,
        annual_time_progress=annual_time_progress,
        non_aggregatable_rate_metrics=non_aggregatable_rates,
    )


def build_department_monthly_report(
    sources: list[dict[str, Any]],
    *,
    period_label: str,
    expected_units: list[str] | None = None,
    generated_for: str = "赵卫中",
    test_mode: bool = False,
) -> str:
    expected = expected_units or REQUIRED_DEPARTMENT_MONTHLY_UNITS
    coverage = department_monthly_coverage(sources, expected_units=expected)
    reports = parse_department_monthly_reports(sources, period_label=period_label)
    summary = aggregate_department_monthly_reports(reports, period_label=period_label)
    return _render_leader_report(
        summary,
        coverage=coverage,
        period_label=period_label,
        generated_for=generated_for,
        test_mode=test_mode,
    )


def department_monthly_coverage(
    sources: list[dict[str, Any]],
    *,
    expected_units: list[str] | None = None,
) -> DepartmentMonthlyCoverage:
    expected = expected_units or REQUIRED_DEPARTMENT_MONTHLY_UNITS
    completed = []
    for unit in expected:
        if any(_source_matches_unit(source, unit) and _source_complete(source) for source in sources):
            completed.append(unit)
    missing = [unit for unit in expected if unit not in completed]
    return DepartmentMonthlyCoverage(
        ready=not missing,
        expected_units=expected,
        completed_units=completed,
        missing_units=missing,
    )


def build_test_department_monthly_sources(period_label: str, *, seed: int = 20260630) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    sources: list[dict[str, Any]] = []
    owners = {
        "法务一部": "一部示例负责人",
        "法务二部": "二部示例负责人",
        "法务三部": "三部示例负责人",
        "法务四部": "四部示例负责人",
        "法务五部": "五部示例负责人",
        "法务六部": "六部示例负责人",
        "综合管理部": "综合管理示例负责人",
        "朱佳佳": "特殊岗位示例负责人",
    }
    for unit_index, unit_name in enumerate(REQUIRED_DEPARTMENT_MONTHLY_UNITS, start=1):
        metrics = template_metrics_for_unit(unit_name)
        sources.append(
            {
                "unit_name": unit_name,
                "owner_name": owners.get(unit_name, f"{unit_name}负责人"),
                "period_label": period_label,
                "status": "completed",
                "confirmed_by_user": True,
                "source_type": "test_structured",
                "metrics": [
                    _fake_metric_response(metric, unit_name=unit_name, rng=rng, unit_index=unit_index, metric_index=index)
                    for index, metric in enumerate(metrics, start=1)
                ],
            }
        )
    return sources


def source_from_performance_submission(
    *,
    unit_name: str,
    owner_name: str,
    period_label: str,
    task_title: str,
    metrics: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    status: str,
    confirmed_by_user: bool,
    source_id: str = "",
) -> dict[str, Any]:
    response_map = {int(item.get("metric_no") or 0): item for item in responses if isinstance(item, dict)}
    merged_metrics: list[dict[str, Any]] = []
    for metric in metrics:
        metric_no = int(metric.get("metric_no") or metric.get("no") or len(merged_metrics) + 1)
        response = dict(response_map.get(metric_no) or {})
        merged_metrics.append(
            {
                "metric_no": metric_no,
                "metric_name": _text(metric.get("name") or metric.get("metric_name") or response.get("metric_name")),
                "unit": _text(metric.get("unit") or response.get("unit")),
                "display_lines": [str(item) for item in metric.get("display_lines", []) if str(item).strip()],
                "reason": _text(response.get("reason")),
                "next_target": _text(response.get("next_target")),
                "actions": _actions(response),
                "raw_text": _text(response.get("raw_text")),
            }
        )
    return {
        "unit_name": unit_name,
        "owner_name": owner_name,
        "period_label": period_label,
        "task_title": task_title,
        "status": status,
        "confirmed_by_user": bool(confirmed_by_user),
        "source_type": "performance_submission",
        "source_id": source_id,
        "metrics": merged_metrics,
    }


def split_markdown_message(text: str, *, max_chars: int = 3600) -> list[str]:
    value = str(text or "")
    if len(value) <= max_chars:
        return [value]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in value.splitlines():
        extra = len(line) + 1
        if current and current_len + extra > max_chars:
            chunks.append("\n".join(current).rstrip())
            current = []
            current_len = 0
        current.append(line)
        current_len += extra
    if current:
        chunks.append("\n".join(current).rstrip())
    return [chunk for chunk in chunks if chunk]


def parse_team_template_metrics_from_paragraphs(paragraphs: list[str]) -> dict[str, list[dict[str, Any]]]:
    section_suffix = "X月度绩效工作汇报"
    current_department = ""
    result: dict[str, list[dict[str, Any]]] = {}
    clean = [str(item or "").strip() for item in paragraphs]
    for index, text in enumerate(clean):
        if not text:
            continue
        if text.endswith(section_suffix):
            current_department = text[: -len(section_suffix)].strip()
            result.setdefault(current_department, [])
            continue
        if not current_department:
            continue
        match = re.match(r"^(\d+)[、](.+)$", text)
        next_non_empty = next((clean[pos] for pos in range(index + 1, min(index + 4, len(clean))) if clean[pos]), "")
        if match and "本月绩效完成情况" in next_non_empty:
            result[current_department].append({"metric_no": int(match.group(1)), "name": match.group(2).strip()})
    return result


def _render_leader_report(
    summary: DepartmentMonthlySummary,
    *,
    coverage: DepartmentMonthlyCoverage,
    period_label: str,
    generated_for: str,
    test_mode: bool,
) -> str:
    title = f"# {'【测试】' if test_mode else ''}法务合约中心{_period_title(period_label)}部门绩效月报"
    lines = [title, "", f"报送对象：{generated_for}"]
    if test_mode:
        lines.extend(["", "> 测试数据，仅用于验证汇总口径、风险分级和发送链路。"])

    lines.extend(["", "## 📌 一、总体结论"])
    for conclusion in _overall_conclusions(summary, coverage):
        lines.append(f"- {conclusion}")

    lines.extend(["", "## 🎯 二、核心指标完成情况"])
    lines.append(f"年度时间进度：{_format_percent(summary.annual_time_progress)}。金额类按合计计算；比例类无分子分母时只做部门展示；分数类标注简单平均。")
    if summary.core_metrics:
        for status in ("red", "yellow", "green"):
            items = [item for item in summary.core_metrics[:14] if item.status == status]
            if not items:
                continue
            lines.extend(["", f"**{_status_group_label(status)}**"])
            for item in items:
                lines.extend(_core_metric_block(item))
    else:
        lines.append("")
        lines.append("暂无可汇总指标，需先补齐月目标、月实际和月完成率。")
    if summary.non_aggregatable_rate_metrics:
        lines.append("")
        lines.append(f"口径提醒：{'、'.join(summary.non_aggregatable_rate_metrics)} 未提供分子分母，不做简单相加。")

    lines.extend(["", "## 🚦 三、重点风险指标"])
    if summary.risk_metrics:
        for metric in summary.risk_metrics[:10]:
            lines.extend(
                [
                f"{_status_label(metric.status)} **{metric.department} - {metric.metric_name}**\n"
                f"- 偏差：{_metric_deviation(metric, summary.annual_time_progress)}。\n"
                f"- 原因：{_safe_sentence(metric.unfinished_reason or '未填写')}。\n"
                f"- 纠偏动作：{_safe_sentence(_first_specific_action(metric.action_plan) or '未填写')}。\n"
                f"- 需领导协调：{'是' if _needs_leadership_coordination(metric) else '否'}。",
                "",
                ]
            )
    else:
        lines.append("- 暂无红色或黄色风险指标。")

    lines.extend(["", "## 四、各部门表现"])
    for report in summary.reports:
        lines.append(f"- **{report.department}**：{_department_performance_sentence(report)}")

    lines.extend(["", "## 🤝 五、需领导协调事项"])
    if summary.leadership_items:
        for index, item in enumerate(summary.leadership_items[:8], start=1):
            lines.extend(_leadership_item_block(index, item))
    else:
        lines.append("暂无需要领导特别协调的事项。")

    lines.extend(["", "## ✅ 六、下月重点动作与风险预判"])
    if summary.key_actions:
        for index, action in enumerate(summary.key_actions[:8], start=1):
            lines.append(f"{index}. {action}")
    else:
        lines.append("1. 先补齐关键指标数据，再形成部门级风险预判。")

    if not coverage.ready:
        lines.extend(["", f"待补齐：{'、'.join(coverage.missing_units)}。"])

    return "\n".join(lines).rstrip()


def _metric_item_from_source_metric(
    metric: dict[str, Any],
    *,
    department: str,
    leader: str,
    annual_time_progress: float,
) -> MetricItem:
    metric_name = _text(metric.get("metric_name") or metric.get("name")) or "未命名指标"
    unit = _text(metric.get("unit"))
    display_lines = [str(item).strip() for item in metric.get("display_lines", []) if str(item).strip()]
    reason = _text(metric.get("reason") or metric.get("unfinished_reason"))
    next_target = _text(metric.get("next_target") or metric.get("next_month_target"))
    actions = _actions(metric)
    raw_text = "\n".join([*display_lines, reason, next_target, *actions, _text(metric.get("raw_text"))]).strip()
    metric_type = _infer_metric_type(metric_name, unit, display_lines)
    fields = _extract_numeric_fields(display_lines, unit=unit, metric_type=metric_type)
    flags = _validation_flags(
        metric_type=metric_type,
        raw_text=raw_text,
        month_target=fields["month_target"],
        month_actual=fields["month_actual"],
        month_completion_rate=fields["month_completion_rate"],
        unfinished_reason=reason,
        actions=actions,
    )
    status = _status_from_rates(
        fields["month_completion_rate"],
        fields["year_completion_rate"],
        annual_time_progress=annual_time_progress,
        critical_missing="关键数据缺失" in flags,
    )
    return MetricItem(
        department=department,
        leader=leader,
        metric_name=metric_name,
        metric_type=metric_type,
        unit=unit or _default_unit_for_type(metric_type),
        month_target=fields["month_target"],
        month_actual=fields["month_actual"],
        month_completion_rate=fields["month_completion_rate"],
        year_target=fields["year_target"],
        year_actual_cumulative=fields["year_actual_cumulative"],
        year_completion_rate=fields["year_completion_rate"],
        yoy_change=fields["yoy_change"],
        mom_change=fields["mom_change"],
        unfinished_reason=reason,
        next_month_target=next_target,
        action_plan=actions,
        raw_text=raw_text,
        metric_no=_to_int(metric.get("metric_no")),
        status=status,
        validation_flags=tuple(flags),
        numerator=_to_float(metric.get("numerator")),
        denominator=_to_float(metric.get("denominator")),
    )


def _core_metric_block(item: MetricAggregate) -> list[str]:
    name = _aggregate_name(item)
    return [
        f"- **{name}**",
        f"  月度：目标 {_format_metric_value(item.month_target, item.unit)}，实际 {_format_metric_value(item.month_actual, item.unit)}，完成率 {_format_percent(item.month_completion_rate)}。",
        f"  年度：目标 {_format_metric_value(item.year_target, item.unit)}，累计 {_format_metric_value(item.year_actual_cumulative, item.unit)}，累计完成率 {_format_percent(item.year_completion_rate)}。",
        f"  同比/环比：{_format_change(item.yoy_change, item)} / {_format_change(item.mom_change, item)}。",
        "",
    ]


def _leadership_item_block(index: int, item: LeadershipCoordinationItem) -> list[str]:
    return [
        f"{index}. **{item.matter}**",
        f"   涉及部门：{item.involved_departments}",
        f"   影响指标：{item.affected_metric}",
        f"   当前卡点：{item.current_bottleneck}",
        f"   需协调动作：{item.coordination_action}",
        f"   建议期限：{item.suggested_deadline}",
    ]


def _extract_numeric_fields(display_lines: list[str], *, unit: str, metric_type: str) -> dict[str, float | None]:
    month_line = _first_line(display_lines, ("月度目标", "单月目标"))
    year_line = _first_line(display_lines, ("年度目标", "全年目标", "累计实际完成", "已完成"))
    all_text = "\n".join(display_lines)
    return {
        "month_target": _extract_number(month_line, [r"月度目标[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|%|分|项|个|件)?", r"单月目标\s*([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|%|分|项|个|件)?"]),
        "month_actual": _extract_number(month_line, [r"实际完成[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|%|分|项|个|件)?", r"完成\s*([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|%|分|项|个|件)?"]),
        "month_completion_rate": _extract_number(month_line, [r"(?:月度目标完成率|完成率)[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*%?"]),
        "year_target": _extract_number(year_line, [r"年度目标[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|%|分|项|个|件)?", r"全年目标\s*([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|%|分|项|个|件)?"]),
        "year_actual_cumulative": _extract_number(year_line, [r"(?:累计实际完成|已完成)[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|%|分|项|个|件)?"]),
        "year_completion_rate": _extract_number(year_line, [r"(?:累计完成率|年度目标完成率)[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*%?"]),
        "yoy_change": _extract_change(all_text, "同比"),
        "mom_change": _extract_change(all_text, "环比"),
    }


def _validation_flags(
    *,
    metric_type: str,
    raw_text: str,
    month_target: float | None,
    month_actual: float | None,
    month_completion_rate: float | None,
    unfinished_reason: str,
    actions: list[str],
) -> list[str]:
    flags: list[str] = []
    if re.search(r"_{2,}|NA#", raw_text):
        flags.append("未填写字段")
    if month_target is None or month_actual is None or month_completion_rate is None:
        flags.append("关键数据缺失")
    if metric_type == "amount" and month_target not in (None, 0) and month_actual is not None and month_completion_rate is not None:
        calculated = month_actual / month_target * 100
        if abs(calculated - month_completion_rate) > 1:
            flags.append("金额完成率不一致")
    if month_completion_rate is not None and month_completion_rate < 100 and _is_empty_reason(unfinished_reason):
        flags.append("完成率低于100但缺少未完成原因")
    if month_completion_rate is not None and month_completion_rate < 100 and not actions:
        flags.append("完成率低于100但缺少行动方案")
    if actions and all(_is_vague_action(action) for action in actions):
        flags.append("行动方案不具体")
    if metric_type == "rate":
        flags.append("比例指标缺少分子分母，禁止汇总")
    return flags


def _aggregate_metric_group(
    metric_name: str,
    metric_type: str,
    items: list[MetricItem],
    annual_time_progress: float,
) -> MetricAggregate | None:
    if metric_type == "amount":
        month_target = _sum_present(item.month_target for item in items)
        month_actual = _sum_present(item.month_actual for item in items)
        year_target = _sum_present(item.year_target for item in items)
        year_actual = _sum_present(item.year_actual_cumulative for item in items)
        month_rate = _rate(month_actual, month_target)
        year_rate = _rate(year_actual, year_target)
        return MetricAggregate(
            metric_name=metric_name,
            metric_type=metric_type,
            unit="万元",
            month_target=month_target,
            month_actual=month_actual,
            month_completion_rate=month_rate,
            year_target=year_target,
            year_actual_cumulative=year_actual,
            year_completion_rate=year_rate,
            yoy_change=_average_present(item.yoy_change for item in items),
            mom_change=_average_present(item.mom_change for item in items),
            status=_status_from_rates(month_rate, year_rate, annual_time_progress=annual_time_progress),
        )
    if metric_type == "count":
        month_target = _sum_present(item.month_target for item in items)
        month_actual = _sum_present(item.month_actual for item in items)
        year_target = _sum_present(item.year_target for item in items)
        year_actual = _sum_present(item.year_actual_cumulative for item in items)
        month_rate = _rate(month_actual, month_target)
        year_rate = _rate(year_actual, year_target)
        return MetricAggregate(
            metric_name=metric_name,
            metric_type=metric_type,
            unit=items[0].unit or "项",
            month_target=month_target,
            month_actual=month_actual,
            month_completion_rate=month_rate,
            year_target=year_target,
            year_actual_cumulative=year_actual,
            year_completion_rate=year_rate,
            yoy_change=_average_present(item.yoy_change for item in items),
            mom_change=_average_present(item.mom_change for item in items),
            status=_status_from_rates(month_rate, year_rate, annual_time_progress=annual_time_progress),
        )
    if metric_type == "score":
        month_target = _average_present(item.month_target for item in items)
        month_actual = _average_present(item.month_actual for item in items)
        year_target = _average_present(item.year_target for item in items)
        year_actual = _average_present(item.year_actual_cumulative for item in items)
        month_rate = _average_present(item.month_completion_rate for item in items)
        year_rate = _average_present(item.year_completion_rate for item in items)
        return MetricAggregate(
            metric_name=metric_name,
            metric_type=metric_type,
            unit="分",
            month_target=month_target,
            month_actual=month_actual,
            month_completion_rate=month_rate,
            year_target=year_target,
            year_actual_cumulative=year_actual,
            year_completion_rate=year_rate,
            yoy_change=_average_present(item.yoy_change for item in items),
            mom_change=_average_present(item.mom_change for item in items),
            status=_status_from_rates(month_rate, year_rate, annual_time_progress=annual_time_progress),
            note="简单平均",
        )
    return None


def _department_rate_metric_row(metric: MetricItem) -> MetricAggregate:
    return MetricAggregate(
        metric_name=f"{metric.department}-{metric.metric_name}",
        metric_type="rate",
        unit=metric.unit or "%",
        month_target=metric.month_target,
        month_actual=metric.month_actual,
        month_completion_rate=metric.month_completion_rate,
        year_target=metric.year_target,
        year_actual_cumulative=metric.year_actual_cumulative,
        year_completion_rate=metric.year_completion_rate,
        yoy_change=metric.yoy_change,
        mom_change=metric.mom_change,
        status=metric.status,
        note="部门展示，未汇总",
    )


def _overall_conclusions(summary: DepartmentMonthlySummary, coverage: DepartmentMonthlyCoverage) -> list[str]:
    metrics = [metric for report in summary.reports for metric in report.metrics]
    amount_metrics = [item for item in summary.core_metrics if item.metric_type == "amount"]
    amount_target = _sum_present(item.month_target for item in amount_metrics)
    amount_actual = _sum_present(item.month_actual for item in amount_metrics)
    amount_rate = _rate(amount_actual, amount_target)
    green = [item.metric_name for item in summary.core_metrics if item.status == "green"]
    risks = [metric for metric in summary.risk_metrics]
    annual_lag = [metric for metric in metrics if metric.year_completion_rate is not None and metric.year_completion_rate < summary.annual_time_progress]
    return [
        f"本月收集情况：已收集 {len(coverage.completed_units)}/{len(coverage.expected_units)} 份，{'全部收齐' if coverage.ready else '待补齐：' + '、'.join(coverage.missing_units)}。",
        f"整体完成情况：金额类可汇总指标月目标 {_format_metric_value(amount_target, '万元')}，月实际 {_format_metric_value(amount_actual, '万元')}，月完成率 {_format_percent(amount_rate)}。",
        f"完成较好的指标：{_join_names(green[:3]) if green else '暂无达到绿色状态的核心指标'}。",
        f"主要短板：{_join_names([f'{item.department}-{item.metric_name}' for item in risks[:3]]) if risks else '暂无红黄风险指标'}。",
        f"年度目标风险：截至{_period_title_short(summary.reports[0].period_label if summary.reports else '2026-06')}，年度时间进度 {_format_percent(summary.annual_time_progress)}，{len(annual_lag)} 个指标累计完成率低于时间进度。",
    ]


def _risk_metrics(metrics: list[MetricItem]) -> list[MetricItem]:
    return sorted(
        [metric for metric in metrics if metric.status in {"red", "yellow"}],
        key=lambda item: (_status_rank(item.status), item.month_completion_rate if item.month_completion_rate is not None else -1),
    )


def _leadership_coordination_items(metrics: list[MetricItem]) -> list[LeadershipCoordinationItem]:
    result: list[LeadershipCoordinationItem] = []
    seen: set[tuple[str, str]] = set()
    for metric in metrics:
        if not _needs_leadership_coordination(metric):
            continue
        key = (metric.department, metric.metric_name)
        if key in seen:
            continue
        seen.add(key)
        party = _coordination_party(metric)
        deadline = _suggested_deadline(metric.action_plan)
        action = _first_specific_action(metric.action_plan) or f"协调{party}明确责任人、排期和交付节点"
        result.append(
            LeadershipCoordinationItem(
                matter=f"{metric.metric_name}推进协调",
                involved_departments=f"{metric.department}、{party}",
                affected_metric=metric.metric_name,
                current_bottleneck=_safe_sentence(metric.unfinished_reason or "关键数据或推进情况未填写"),
                coordination_action=_safe_sentence(action),
                suggested_deadline=deadline,
            )
        )
    return result


def _key_department_actions(reports: list[DepartmentMonthlyReport], risk_metrics: list[MetricItem]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    source_metrics = risk_metrics + [metric for report in reports for metric in report.metrics]
    for metric in source_metrics:
        action = _first_specific_action(metric.action_plan)
        if not action:
            continue
        text = f"{metric.department}-{metric.metric_name}：{action}"
        normalized = _compact(text)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(text)
        if len(result) >= 8:
            break
    return result


def _department_performance_sentence(report: DepartmentMonthlyReport) -> str:
    greens = [metric.metric_name for metric in report.metrics if metric.status == "green"]
    risks = [metric.metric_name for metric in report.metrics if metric.status in {"red", "yellow"}]
    action = _first_specific_action([action for metric in report.metrics for action in metric.action_plan])
    bright = _join_names(greens[:2]) if greens else "暂无明显超额项"
    short = _join_names(risks[:2]) if risks else "暂无突出短板"
    next_action = action or "补齐关键指标数据并明确下月动作"
    return f"亮点：{bright}。短板：{short}。下月关键动作：{next_action}。"


def _fake_metric_response(
    metric: dict[str, Any],
    *,
    unit_name: str,
    rng: random.Random,
    unit_index: int,
    metric_index: int,
) -> dict[str, Any]:
    metric_name = _text(metric.get("name"))
    unit = _text(metric.get("unit"))
    metric_type = _infer_metric_type(metric_name, unit, [])
    display_lines = _fake_display_lines(metric, metric_type=metric_type, unit=unit, rng=rng, unit_index=unit_index, metric_index=metric_index)
    reason = _fake_reason(metric_name, metric_type, rng=rng)
    actions = _fake_actions(metric_name, reason=reason, rng=rng)
    return {
        "metric_no": int(metric.get("metric_no") or metric_index),
        "metric_name": metric_name,
        "unit": unit,
        "display_lines": display_lines,
        "reason": reason,
        "next_target": _fake_target(metric, metric_type=metric_type, unit=unit, rng=rng, unit_index=unit_index),
        "actions": actions,
        "source_unit": unit_name,
    }


def _fake_display_lines(
    metric: dict[str, Any],
    *,
    metric_type: str,
    unit: str,
    rng: random.Random,
    unit_index: int,
    metric_index: int,
) -> list[str]:
    status_roll = (unit_index + metric_index) % 4
    if metric_type == "amount":
        year_target = _to_float(metric.get("year_target")) or (unit_index * 1000 + metric_index * 500)
        month_target = max(10.0, round(year_target / 12, 2))
        ratio = [1.12, 0.93, 0.76, 1.02][status_roll]
        month_actual = round(month_target * ratio, 2)
        year_ratio = [0.54, 0.46, 0.31, 0.52][status_roll]
        year_actual = round(year_target * year_ratio, 2)
        return [
            f"月度目标：{_clean_number(month_target)}万元，实际完成：{_clean_number(month_actual)}万元，完成率：{_clean_number(_rate(month_actual, month_target))}%，同比上升/下降：{rng.randint(-12, 15)}%，环比上升/下降：{rng.randint(-10, 12)}%；",
            f"年度目标：{_clean_number(year_target)}万元，累计实际完成：{_clean_number(year_actual)}万元，累计完成率：{_clean_number(_rate(year_actual, year_target))}%，同比上升/下降：{rng.randint(-15, 12)}%；",
        ]
    if metric_type == "score":
        year_target = _to_float(metric.get("year_target")) or 90
        month_target = year_target
        month_actual = round(month_target * [1.02, 0.91, 0.78, 1.0][status_roll], 1)
        year_actual = round(year_target * [0.52, 0.45, 0.35, 0.51][status_roll], 1)
        return [
            f"月度目标：{_clean_number(month_target)}分，实际完成：{_clean_number(month_actual)}分，完成率：{_clean_number(_rate(month_actual, month_target))}%，同比上升/下降：{rng.randint(-4, 8)}分，环比上升/下降：{rng.randint(-3, 6)}分；",
            f"年度目标：{_clean_number(year_target)}分，累计实际完成：{_clean_number(year_actual)}分，累计完成率：{_clean_number(_rate(year_actual, year_target))}%，同比上升/下降：{rng.randint(-4, 8)}分；",
        ]
    if metric_type == "rate":
        year_target = _to_float(metric.get("year_target")) or 100
        month_target = min(100.0, year_target)
        month_actual = round(month_target * [1.03, 0.88, 0.68, 1.0][status_roll], 2)
        year_actual = round(year_target * [0.53, 0.44, 0.32, 0.51][status_roll], 2)
        return [
            f"月度目标：{_clean_number(month_target)}%，实际完成：{_clean_number(month_actual)}%，完成率：{_clean_number(_rate(month_actual, month_target))}%，同比上升/下降：{rng.randint(-4, 8)}百分点，环比上升/下降：{rng.randint(-3, 6)}百分点；",
            f"年度目标：{_clean_number(year_target)}%，累计实际完成：{_clean_number(year_actual)}%，累计完成率：{_clean_number(_rate(year_actual, year_target))}%，同比上升/下降：{rng.randint(-4, 8)}百分点；",
        ]
    year_target = _to_float(metric.get("year_target")) or max(10, metric_index * 4)
    month_target = max(1.0, round(year_target / 12, 2))
    month_actual = round(month_target * [1.1, 0.9, 0.75, 1.0][status_roll], 2)
    year_actual = round(year_target * [0.52, 0.45, 0.36, 0.51][status_roll], 2)
    unit_label = unit or "项"
    return [
        f"月度目标：{_clean_number(month_target)}{unit_label}，实际完成：{_clean_number(month_actual)}{unit_label}，完成率：{_clean_number(_rate(month_actual, month_target))}%；",
        f"年度目标：{_clean_number(year_target)}{unit_label}，累计实际完成：{_clean_number(year_actual)}{unit_label}，累计完成率：{_clean_number(_rate(year_actual, year_target))}%；",
    ]


def _fake_reason(metric_name: str, metric_type: str, *, rng: random.Random) -> str:
    pool = [
        "客户资料回收较慢，部分审批节点需业务和项目共同确认",
        "供应商接口联调延期，系统流程衔接未按计划完成",
        "历史案件基数较高，部分回款节点受法院和客户排期影响",
        "跨部门数据口径尚未统一，影响月度完成情况核验",
        "重点项目推进正常，但年度累计进度低于时间进度",
    ]
    if metric_type == "amount":
        pool.append("部分项目付款审批链条较长，现金回款确认晚于计划")
    if "AI" in metric_name or "智能" in metric_name:
        pool.append("试点场景验收依赖信息化资源，供应商交付节奏偏慢")
    return rng.choice(pool)


def _fake_actions(metric_name: str, *, reason: str, rng: random.Random) -> list[str]:
    actions = [
        "7月5日前形成红黄灯清单并明确责任人",
        "每周三向部门同步一次完成率和卡点",
        "7月15日前完成关键资料补齐和口径复核",
        "7月底前完成重点项目专项复盘",
        "7月10日前由相关部门确认审批或联调排期",
    ]
    if "供应商" in reason or "信息化" in reason:
        actions.append("7月10日前由信息化、采购、供应商确认联调排期")
    if "法院" in reason:
        actions.append("7月12日前由案件负责人完成法院沟通计划")
    rng.shuffle(actions)
    return actions[:3]


def _fake_target(metric: dict[str, Any], *, metric_type: str, unit: str, rng: random.Random, unit_index: int) -> str:
    if metric_type == "amount":
        return f"{unit_index * 100 + rng.randint(20, 120)}万元"
    if metric_type == "rate":
        return f"{rng.randint(80, 100)}%"
    if metric_type == "score":
        return f"{rng.randint(86, 96)}分"
    return f"{rng.randint(2, 8)}{unit or '项'}"


def _metric_template(metric_no: int, name: str, unit: str, year_target: Any) -> dict[str, Any]:
    display_unit = unit or ""
    year_target_text = str(year_target) if year_target not in (None, "") else "______"
    return {
        "metric_no": metric_no,
        "name": name,
        "unit": unit,
        "year_target": year_target,
        "display_lines": [
            f"月度目标：______{display_unit}，实际完成：______{display_unit}，完成率：______%；",
            f"年度目标：{year_target_text}{display_unit}，累计实际完成：______{display_unit}，累计完成率：______%；",
        ],
    }


def _infer_metric_type(metric_name: str, unit: str, display_lines: list[str]) -> str:
    text = f"{metric_name} {unit} {' '.join(display_lines)}"
    if "万元" in text or "亿元" in text:
        return "amount"
    if unit == "分" or "分" in unit:
        return "score"
    if unit == "%" or "率" in metric_name or "覆盖" in metric_name or "自动化程度" in metric_name:
        return "rate"
    if any(token in text for token in ("项", "个", "件")):
        return "count"
    return "count"


def _default_unit_for_type(metric_type: str) -> str:
    return {"amount": "万元", "rate": "%", "score": "分", "count": "项"}.get(metric_type, "")


def _annual_time_progress(period_label: str) -> float:
    match = re.search(r"(\d{4})[-年/.](\d{1,2})", str(period_label or ""))
    if not match:
        return 50.0
    month = min(12, max(1, int(match.group(2))))
    return round(month / 12 * 100, 2)


def _status_from_rates(
    month_rate: float | None,
    year_rate: float | None,
    *,
    annual_time_progress: float,
    critical_missing: bool = False,
) -> str:
    if critical_missing:
        return "red"
    if month_rate is None and year_rate is None:
        return "red"
    if month_rate is not None and month_rate < 80:
        return "red"
    if year_rate is not None and year_rate < annual_time_progress - 10:
        return "red"
    if month_rate is not None and month_rate < 100:
        return "yellow"
    if year_rate is not None and year_rate < annual_time_progress:
        return "yellow"
    if (month_rate is None or month_rate >= 100) and (year_rate is None or year_rate >= annual_time_progress):
        return "green"
    return "yellow"


def _extract_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if not match:
            continue
        value = _to_float(match.group(1))
        if value is None:
            continue
        unit = match.group(2) if len(match.groups()) >= 2 else ""
        if unit == "亿元":
            return value * 10000
        return value
    return None


def _extract_change(text: str, label: str) -> float | None:
    match = re.search(label + r"(?:上升/下降)?[:：]\s*([+-]?\d+(?:\.\d+)?)", text or "")
    if match:
        return _to_float(match.group(1))
    match = re.search(label + r"(增长|上升|降低|下降)?\s*([+-]?\d+(?:\.\d+)?)", text or "")
    if not match:
        return None
    value = _to_float(match.group(2))
    if value is None:
        return None
    direction = match.group(1) or ""
    if direction in {"降低", "下降"} and value > 0:
        return -value
    return value


def _first_line(lines: list[str], keywords: tuple[str, ...]) -> str:
    return next((line for line in lines if any(keyword in line for keyword in keywords)), "")


def _rate(actual: float | None, target: float | None) -> float | None:
    if actual is None or target in (None, 0):
        return None
    return round(actual / target * 100, 2)


def _sum_present(values: Any) -> float | None:
    nums = [value for value in values if value is not None]
    if not nums:
        return None
    return round(sum(nums), 2)


def _average_present(values: Any) -> float | None:
    nums = [value for value in values if value is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _needs_leadership_coordination(metric: MetricItem) -> bool:
    text = _compact(" ".join([metric.unfinished_reason, " ".join(metric.action_plan)]))
    keywords = ("跨部门", "供应商", "信息化", "采购", "客户", "法院", "审批", "资源", "系统", "协同", "协调")
    internal_only = ("内部复盘", "部门内部", "自行", "本部门")
    return metric.status in {"red", "yellow"} and any(keyword in text for keyword in keywords) and not any(word in text for word in internal_only)


def _coordination_party(metric: MetricItem) -> str:
    text = metric.unfinished_reason + " " + " ".join(metric.action_plan)
    parties = []
    if any(word in text for word in ("信息化", "系统", "接口", "智能")):
        parties.append("信息化")
    if "采购" in text:
        parties.append("采购")
    if "供应商" in text:
        parties.append("供应商")
    if "客户" in text:
        parties.append("客户/业务负责人")
    if "法院" in text:
        parties.append("案件承办人与法院沟通窗口")
    if "审批" in text and "客户/业务负责人" not in parties:
        parties.append("相关审批部门")
    if not parties:
        parties.append("相关协同部门")
    if "供应商" in parties and len(parties) > 1:
        others = [party for party in parties if party != "供应商"]
        return "/".join(others) + "、供应商"
    return "/".join(parties)


def _suggested_deadline(actions: list[str]) -> str:
    text = " ".join(actions)
    match = re.search(r"(\d{1,2}月\d{1,2}日前)", text)
    if match:
        return match.group(1)
    match = re.search(r"(\d{1,2}月底前)", text)
    if match:
        return match.group(1)
    return "下月15日前"


def _metric_deviation(metric: MetricItem, annual_time_progress: float) -> str:
    parts = []
    if metric.month_completion_rate is not None:
        parts.append(f"月完成率{_format_percent(metric.month_completion_rate)}")
    if metric.year_completion_rate is not None:
        delta = round(metric.year_completion_rate - annual_time_progress, 2)
        direction = "高于" if delta >= 0 else "低于"
        parts.append(f"累计完成率{_format_percent(metric.year_completion_rate)}，{direction}时间进度{_format_percent(abs(delta))}")
    if metric.validation_flags:
        parts.append("；".join(metric.validation_flags))
    return "；".join(parts) if parts else "关键数据缺失"


def _status_label(status: str) -> str:
    return {"green": "🟢 绿", "yellow": "🟡 黄", "red": "🔴 红"}.get(status, "🔴 红")


def _status_group_label(status: str) -> str:
    return {"green": "🟢 达成较好", "yellow": "🟡 需要关注", "red": "🔴 重点风险"}.get(status, "🔴 重点风险")


def _status_rank(status: str) -> int:
    return {"red": 0, "yellow": 1, "green": 2}.get(status, 3)


def _format_metric_value(value: float | None, unit: str) -> str:
    if value is None:
        return "未填写"
    suffix = unit or ""
    return f"{_clean_number(value)}{suffix}"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "未填写"
    return f"{_clean_number(value)}%"


def _format_change(value: float | None, item: MetricAggregate) -> str:
    if value is None:
        return "未填写"
    suffix = "分" if item.metric_type == "score" else ("百分点" if item.metric_type == "rate" else "%")
    return f"{_clean_number(value)}{suffix}"


def _aggregate_name(item: MetricAggregate) -> str:
    return f"{item.metric_name}（{item.note}）" if item.note else item.metric_name


def _table_cell(value: Any) -> str:
    return str(value or "未填写").replace("|", "/").replace("\n", " ").strip()


def _safe_sentence(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().rstrip("；;。")


def _first_specific_action(actions: list[str]) -> str:
    for action in actions:
        if action and not _is_vague_action(action):
            return action
    return ""


def _is_empty_reason(reason: str) -> bool:
    return _compact(reason) in {"", "无", "暂无", "已完成", "无问题", "不存在"}


def _is_vague_action(action: str) -> bool:
    compact = _compact(action)
    vague = ("加强推进", "持续跟进", "加大力度", "形成机制", "继续努力", "积极推进", "尽快推进")
    return len(compact) <= 6 or any(word in compact for word in vague)


def _join_names(values: list[str]) -> str:
    return "、".join(values) if values else ""


def _period_title(period_label: str) -> str:
    match = re.search(r"(\d{4})[-年/.](\d{1,2})", str(period_label or ""))
    if not match:
        return str(period_label)
    return f"{match.group(1)}年{int(match.group(2))}月"


def _period_title_short(period_label: str) -> str:
    match = re.search(r"(\d{4})[-年/.](\d{1,2})", str(period_label or ""))
    if not match:
        return str(period_label)
    return f"{int(match.group(2))}月"


def _source_matches_unit(source: dict[str, Any], unit: str) -> bool:
    text = f"{source.get('unit_name', '')} {source.get('task_title', '')} {source.get('owner_name', '')}"
    return unit in text


def _source_complete(source: dict[str, Any]) -> bool:
    return str(source.get("status") or "") == "completed" and bool(source.get("confirmed_by_user"))


def _actions(metric: dict[str, Any]) -> list[str]:
    value = metric.get("actions") or metric.get("action_plan") or []
    if isinstance(value, str):
        parts = re.split(r"[\n；;]+|(?<=\D)[1-4][.、]", value)
        return [part.strip() for part in parts if part.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _compact(value: str) -> str:
    return re.sub(r"[\s，。、“”‘’；;：:,.!?！？（）()\[\]【】\-_/]+", "", str(value or "").lower())


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _clean_number(value: float | int | None) -> str:
    if value is None:
        return "未填写"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")
