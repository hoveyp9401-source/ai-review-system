from __future__ import annotations

import copy
import io
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from app.legal_ops_data_intake.calculator import resolve_lookup_path
from app.legal_ops_data_intake.workbook import safe_excel_cell


class PerformanceReportError(ValueError):
    """The selected rule or source data cannot produce an auditable report."""


@dataclass(frozen=True)
class DefendantPerformanceReport:
    _payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._payload)


@dataclass(frozen=True)
class _FieldRef:
    key: str
    source_name: str


@dataclass(frozen=True)
class _CaseRow:
    team_name: str
    lawyer_name: str
    branch_name: str
    case_name: str
    register_date: date
    is_closed: bool
    close_date: date | None
    source_row_number: int | None


@dataclass(frozen=True)
class _LossRow:
    is_closed: bool
    close_date: date | None
    opponent_type: str
    loss_reduction: Decimal | None
    case_reason: str
    subject_amount: Decimal | None
    interest: Decimal | None
    total_principal: Decimal | None
    total_appraisal: Decimal | None
    total_litigation: Decimal | None
    total_penalty: Decimal | None


@dataclass(frozen=True)
class _ReportPeriod:
    view: Literal["week", "month"]
    starts_on: date
    ends_on: date
    previous_cutoff: date
    last_year_cutoff: date
    label: str
    comparison_label: str

    def as_dict(self) -> dict[str, str]:
        return {
            "view": self.view,
            "starts_on": self.starts_on.isoformat(),
            "ends_on": self.ends_on.isoformat(),
            "cutoff_date": self.ends_on.isoformat(),
            "label": self.label,
            "comparison_label": self.comparison_label,
        }


def build_defendant_performance_report(
    rule_spec: dict[str, Any],
    sources: dict[str, list[dict[str, Any]]],
    *,
    view: Literal["week", "month"],
    anchor_date: date,
    targets: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> DefendantPerformanceReport:
    """Build one read-only weekly or monthly defendant performance report."""

    period = _report_period(view, anchor_date)
    table_key, fields = _case_table_definition(rule_spec)
    rows = list(sources.get(table_key) or ())
    lookup = _subject_lookup(rule_spec, table_key)
    excluded_teams = {
        str(value).strip()
        for value in (rule_spec.get("subject") or {}).get("exclude_values") or ()
        if str(value).strip()
    }
    cases: list[_CaseRow] = []
    assignment_errors: list[dict[str, Any]] = []
    data_quality_errors: list[dict[str, Any]] = []
    for row in rows:
        is_closed = _is_closed(_field_value(row, fields["is_closed"]))
        close_date = _optional_date(_field_value(row, fields["close_date"]))
        if is_closed and close_date is None:
            data_quality_errors.append(
                {
                    "source_row_number": _row_number(row),
                    "error_type": "closed_without_close_date",
                    "message": "已标记结案但结案日期为空，不计入存量",
                    "case_name": _case_name(row),
                }
            )
        resolution = resolve_lookup_path(lookup, row)
        if resolution.error:
            assignment_errors.append(
                {
                    "source_row_number": _row_number(row),
                    "step_name": str(resolution.error.get("step_name") or ""),
                    "source_value": str(resolution.error.get("value") or ""),
                    "message": _assignment_error_message(resolution.error),
                }
            )
            continue
        team_name = str(resolution.final_value or "").strip()
        if not team_name or team_name in excluded_teams:
            continue
        register_date = _optional_date(_field_value(row, fields["register_date"]))
        if register_date is None:
            continue
        lawyer_name = (
            _primary_lawyer_name(resolution.steps[0].mapped_value)
            if resolution.steps
            else ""
        )
        cases.append(
            _CaseRow(
                team_name=team_name,
                lawyer_name=lawyer_name,
                branch_name=str(
                    _field_value(row, fields["branch_name"]) or ""
                ).strip(),
                case_name=_case_name(row),
                register_date=register_date,
                is_closed=is_closed,
                close_date=close_date,
                source_row_number=_row_number(row),
            )
        )

    loss_metrics = (
        _loss_metrics(rows, fields, period)
        if period.view == "month"
        else _weekly_loss_not_applicable()
    )
    # Unresolved ownership changes which team receives a case, so it blocks a
    # target conclusion. A closed row without a close date is still handled by
    # the Skill's fixed rule (excluded from stock) and remains traceable as a
    # maintenance warning, but it does not make the configured target unknown.
    target_data_incomplete = bool(assignment_errors)
    teams = sorted({item.team_name for item in cases})
    total_key = str((rule_spec.get("subject") or {}).get("total_key") or "__total__")
    total_label = str((rule_spec.get("subject") or {}).get("total_label") or "整体")
    scopes = [
        _scope_report(
            total_key,
            total_label,
            cases,
            period,
            targets,
            scope_type="overall",
            data_incomplete=target_data_incomplete,
            loss_metrics=loss_metrics,
        )
    ]
    scopes.extend(
        _scope_report(
            team,
            team,
            [item for item in cases if item.team_name == team],
            period,
            targets,
            scope_type="team",
            data_incomplete=target_data_incomplete,
        )
        for team in teams
    )
    lawyers = sorted({item.lawyer_name for item in cases if item.lawyer_name})
    personal_scopes = [
        _scope_report(
            f"person:{lawyer}",
            lawyer,
            [item for item in cases if item.lawyer_name == lawyer],
            period,
            targets,
            scope_type="person",
            data_incomplete=target_data_incomplete,
        )
        for lawyer in lawyers
    ]
    return DefendantPerformanceReport(
        {
            "period": period.as_dict(),
            "source_table_key": table_key,
            "source_row_count": len(rows),
            "mapped_row_count": len(cases),
            "assignment_error_count": len(assignment_errors),
            "data_quality_error_count": len(data_quality_errors),
            "target_conclusions_available": not target_data_incomplete,
            "loss_metrics": loss_metrics,
            "scopes": scopes,
            "personal_scopes": personal_scopes,
            "assignment_errors": assignment_errors,
            "data_quality_errors": data_quality_errors,
        }
    )


def export_defendant_performance_xlsx(
    report: DefendantPerformanceReport,
    *,
    scope_key: str,
) -> bytes:
    """Export the same three-sheet business workbook used by team leaders."""

    payload = report.as_dict()
    scope = _selected_scope(payload, scope_key)
    period = dict(payload["period"])
    weekly = str(period.get("view") or "") == "week"
    period_new_label = "本周新增" if weekly else "本月新增"
    period_closed_label = "本周结案" if weekly else "本月结案"
    workbook = Workbook()
    stock = workbook.active
    stock.title = "存量"
    additions = workbook.create_sheet("新增")
    combined = workbook.create_sheet("综合汇总")
    export_sheets = [stock, additions, combined]

    stock.append(
        ["分公司", "承办法务", "存量（件）", "去年存量", "存量同比", "存量环比"]
    )
    additions.append(
        [
            "分公司",
            "承办法务",
            "YTD新增（件）",
            "去年YTD新增",
            "新增同比",
            period_new_label,
        ]
    )
    combined.append(
        [
            "分公司",
            "承办法务",
            "存量",
            "存量同比",
            "存量环比",
            "YTD新增",
            "新增同比",
            period_new_label,
            period_closed_label,
        ]
    )
    for branch in scope["branches"]:
        stock.append(
            [
                safe_excel_cell(branch["branch_name"]),
                safe_excel_cell(branch["lawyer_name"]),
                int(branch["stock_count"]),
                int(branch["last_year_stock_count"]),
                _arrow_rate(branch["stock_yoy"]),
                _arrow_rate(branch["stock_period_change"]),
            ]
        )
        additions.append(
            [
                safe_excel_cell(branch["branch_name"]),
                safe_excel_cell(branch["lawyer_name"]),
                int(branch["year_to_date_new_count"]),
                int(branch["last_year_to_date_new_count"]),
                _arrow_rate(branch["new_yoy"]),
                int(branch["period_new_count"]),
            ]
        )
        combined.append(
            [
                safe_excel_cell(branch["branch_name"]),
                safe_excel_cell(branch["lawyer_name"]),
                int(branch["stock_count"]),
                _arrow_rate(branch["stock_yoy"]),
                _arrow_rate(branch["stock_period_change"]),
                int(branch["year_to_date_new_count"]),
                _arrow_rate(branch["new_yoy"]),
                int(branch["period_new_count"]),
                int(branch["period_closed_count"]),
            ]
        )
    stock.append(
        [
            "合计",
            None,
            int(scope["stock_count"]),
            int(scope["last_year_stock_count"]),
            _arrow_rate(scope["stock_yoy"]),
            _arrow_rate(scope["stock_period_change"]),
        ]
    )
    additions.append(
        [
            "合计",
            None,
            int(scope["year_to_date_new_count"]),
            int(scope["last_year_to_date_new_count"]),
            _arrow_rate(scope["new_yoy"]),
            int(scope["period_new_count"]),
        ]
    )
    combined.append(
        [
            "合计",
            None,
            int(scope["stock_count"]),
            _arrow_rate(scope["stock_yoy"]),
            _arrow_rate(scope["stock_period_change"]),
            int(scope["year_to_date_new_count"]),
            _arrow_rate(scope["new_yoy"]),
            int(scope["period_new_count"]),
            int(scope["period_closed_count"]),
        ]
    )
    if not weekly and scope.get("scope_type") == "overall":
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
        loss = workbook.create_sheet("减损")
        loss.append(
            [
                "指标",
                "结果",
                "纳入案件数",
                "标的额及利息合计（元）",
                "应付合计（元）",
            ]
        )
        loss.append(
            [
                "综合减损率",
                comprehensive.get("display")
                or comprehensive.get("status_label")
                or "暂不可计算",
                comprehensive.get("eligible_case_count"),
                _excel_amount(comprehensive.get("claim_amount")),
                _excel_amount(comprehensive.get("payable_amount")),
            ]
        )
        loss.append(
            [
                "实质减损金额",
                substantial.get("display")
                or substantial.get("status_label")
                or "暂不可计算",
                substantial.get("eligible_case_count"),
                None,
                None,
            ]
        )
        export_sheets.append(loss)
    for sheet in export_sheets:
        _style_export_sheet(sheet)
    warning_rows = _export_warning_rows(payload)
    if warning_rows:
        warning_sheet = workbook.create_sheet("数据完整性提示", 0)
        warning_sheet.append(["问题类型", "原表行", "案件/归属值", "说明"])
        for warning in warning_rows:
            warning_sheet.append(
                [
                    safe_excel_cell(warning["type"]),
                    warning["source_row_number"],
                    safe_excel_cell(warning["subject"]),
                    safe_excel_cell(warning["message"]),
                ]
            )
        _style_export_sheet(warning_sheet)
        warning_sheet.sheet_properties.tabColor = "D97706"
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def export_defendant_performance_docx(
    report: DefendantPerformanceReport,
    *,
    scope_key: str,
) -> bytes:
    """Export the readable Word report used for weekly/monthly briefings."""

    payload = report.as_dict()
    scope = _selected_scope(payload, scope_key)
    period = dict(payload["period"])
    weekly = str(period.get("view") or "") == "week"
    period_name = "周" if weekly else "月"
    current_label = "本周" if weekly else "本月"
    scope_label = (
        "法务部门整体" if scope["scope_name"] == "整体" else str(scope["scope_name"])
    )
    starts_on = date.fromisoformat(str(period["starts_on"]))
    ends_on = date.fromisoformat(str(period["ends_on"]))
    document = Document()
    _configure_document_styles(document)
    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title.add_run(f"{scope_label}被告案件{period_name}报")
    title_run.bold = True
    title_run.font.size = Pt(18)

    document.add_paragraph(
        "统计周期："
        f"{starts_on.year}年{starts_on.month}月{starts_on.day}日"
        f" — {ends_on.month}月{ends_on.day}日"
        f"（截止{ends_on:%m月%d日}）"
    )
    document.add_paragraph("（归属口径：底表分公司→对接人→法务团队映射链）")
    warning_rows = _export_warning_rows(payload)
    if warning_rows:
        warning = document.add_paragraph()
        warning_text = (
            f"数据完整性提醒：有 {len(warning_rows)} 条记录待确认。"
        )
        if not bool(payload.get("target_conclusions_available", True)):
            warning_text += (
                "当前数量仍可查看，但“达到/未达到目标”结论暂不展示。"
            )
        else:
            warning_text += "目标结论仍按已配置的确定性口径展示。"
        run = warning.add_run(warning_text)
        run.bold = True
        run.font.color.rgb = RGBColor(176, 82, 26)
        for item in warning_rows[:10]:
            document.add_paragraph(
                f"原表第{item['source_row_number'] or '—'}行："
                f"{item['subject']}；{item['message']}",
                style="List Bullet",
            )

    _add_section_heading(document, "一、存量")
    document.add_paragraph(
        f"截至{ends_on:%m月%d日}，{scope_label}被告案件存量 "
        f"{scope['stock_count']} 件，同比{_sentence_rate(scope['stock_yoy'])}，"
        f"环比{_sentence_rate(scope['stock_period_change'])}。"
    )
    label = document.add_paragraph()
    label.add_run("存量明细（按分公司）：").bold = True
    stock_rows = [
        [
            branch["branch_name"],
            branch["lawyer_name"],
            branch["stock_count"],
            branch["last_year_stock_count"],
            _arrow_rate(branch["stock_yoy"]),
            _arrow_rate(branch["stock_period_change"]),
        ]
        for branch in scope["branches"]
    ]
    stock_rows.append(
        [
            "合计",
            "",
            scope["stock_count"],
            scope["last_year_stock_count"],
            _arrow_rate(scope["stock_yoy"]),
            _arrow_rate(scope["stock_period_change"]),
        ]
    )
    _add_business_table(
        document,
        ["分公司", "承办法务", "存量（件）", "去年同期", "同比", "环比"],
        stock_rows,
    )

    _add_section_heading(document, "二、新增")
    document.add_paragraph(
        f"本年度累计（1月1日-{ends_on:%m月%d日}）新增 "
        f"{scope['year_to_date_new_count']} 件，"
        f"同比{_sentence_rate(scope['new_yoy'])}。"
    )
    document.add_paragraph(
        f"{current_label}（{starts_on.month}/{starts_on.day}-"
        f"{ends_on.month}/{ends_on.day}）新增 "
        f"{scope['period_new_count']} 件。"
    )
    label = document.add_paragraph()
    label.add_run("新增明细（按分公司）：").bold = True
    new_rows = [
        [
            branch["branch_name"],
            branch["lawyer_name"],
            branch["year_to_date_new_count"],
            branch["last_year_to_date_new_count"],
            _arrow_rate(branch["new_yoy"]),
            branch["period_new_count"],
        ]
        for branch in scope["branches"]
    ]
    new_rows.append(
        [
            "合计",
            "",
            scope["year_to_date_new_count"],
            scope["last_year_to_date_new_count"],
            _arrow_rate(scope["new_yoy"]),
            scope["period_new_count"],
        ]
    )
    _add_business_table(
        document,
        [
            "分公司",
            "承办法务",
            "YTD新增（件）",
            "去年YTD",
            "同比",
            f"{current_label}新增",
        ],
        new_rows,
    )

    next_section = 3
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
        scope.get("scope_type") == "overall"
        and comprehensive.get("status") == "calculated"
        and substantial.get("status") == "calculated"
    ):
        _add_section_heading(document, "三、减损情况（部门整体口径）")
        document.add_paragraph(
            f"{current_label}被告案件综合减损率 "
            f"{comprehensive.get('display') or '暂不可计算'}；"
            "实质减损金额（本年度累计）"
            f"{substantial.get('display') or '暂不可计算'}。"
        )
        next_section = 4

    period_dates = f"{starts_on.month}/{starts_on.day}-{ends_on.month}/{ends_on.day}"
    _add_section_heading(
        document,
        f"{_chinese_section_number(next_section)}、"
        f"{current_label}结案明细（{period_dates}）",
    )
    document.add_paragraph(f"{current_label}结案 {scope['period_closed_count']} 件。")
    if scope["period_closed_cases"]:
        _add_business_table(
            document,
            ["分公司", "对接法务", "案件名称"],
            [
                [
                    item["branch_name"],
                    item["lawyer_name"],
                    item["case_name"],
                ]
                for item in scope["period_closed_cases"]
            ],
        )

    _add_section_heading(
        document,
        f"{_chinese_section_number(next_section + 1)}、"
        f"{current_label}新增明细（{period_dates}）",
    )
    document.add_paragraph(f"{current_label}新增 {scope['period_new_count']} 件。")
    if scope["period_new_cases"]:
        _add_business_table(
            document,
            ["分公司", "对接法务", "案件名称"],
            [
                [
                    item["branch_name"],
                    item["lawyer_name"],
                    item["case_name"],
                ]
                for item in scope["period_new_cases"]
            ],
        )

    document.core_properties.title = f"{scope_label}被告案件{period_name}报"
    document.core_properties.subject = "被告绩效指标及案件明细"
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def _report_period(
    view: Literal["week", "month"],
    anchor_date: date,
) -> _ReportPeriod:
    if not isinstance(anchor_date, date):
        raise PerformanceReportError("统计截止日期无效")
    if view == "week":
        starts_on = anchor_date - timedelta(days=anchor_date.weekday())
        friday = starts_on + timedelta(days=4)
        ends_on = min(anchor_date, friday)
        first_day = starts_on.replace(day=1)
        week_number = ((starts_on.day + first_day.weekday() - 1) // 7) + 1
        return _ReportPeriod(
            view="week",
            starts_on=starts_on,
            ends_on=ends_on,
            previous_cutoff=starts_on - timedelta(days=3),
            last_year_cutoff=_shift_year(ends_on, -1),
            label=f"{starts_on.year}年{starts_on.month}月第{week_number}周",
            comparison_label="上周五",
        )
    if view == "month":
        starts_on = anchor_date.replace(day=1)
        return _ReportPeriod(
            view="month",
            starts_on=starts_on,
            ends_on=anchor_date,
            previous_cutoff=starts_on - timedelta(days=1),
            last_year_cutoff=_shift_year(anchor_date, -1),
            label=f"{anchor_date.year}年{anchor_date.month}月",
            comparison_label="上月末",
        )
    raise PerformanceReportError("只支持周维度或月维度")


def _case_table_definition(
    rule_spec: dict[str, Any],
) -> tuple[str, dict[str, _FieldRef]]:
    required_names = {
        "branch_name": "分公司名",
        "register_date": "系统登记日期",
        "is_closed": "是否结案",
        "close_date": "结案日期",
    }
    optional_names = {
        "opponent_type": "对方当事人单位/个人",
        "loss_reduction": "减损金额",
        "case_reason": "案情原因",
        "subject_amount": "标的额",
        "interest": "利息",
        "total_principal": "总应付本金",
        "total_appraisal": "总应付鉴定费",
        "total_litigation": "总应付诉讼费",
        "total_penalty": "总应付违约金",
    }
    for table in rule_spec.get("source_tables") or ():
        columns = {
            str(column.get("name") or "").strip(): _FieldRef(
                key=str(column.get("key") or "").strip(),
                source_name=str(column.get("name") or "").strip(),
            )
            for column in table.get("columns") or ()
            if isinstance(column, dict)
        }
        if all(name in columns for name in required_names.values()):
            fields = {
                key: columns[name] for key, name in required_names.items()
            }
            fields.update(
                {
                    key: columns[name]
                    for key, name in optional_names.items()
                    if name in columns
                }
            )
            return str(table.get("key") or ""), fields
    raise PerformanceReportError(
        "当前 Skill 未完整声明分公司、登记日期、结案状态和结案日期，暂不能生成被告绩效报告"
    )


def _subject_lookup(
    rule_spec: dict[str, Any],
    table_key: str,
) -> dict[str, Any]:
    lookup = (rule_spec.get("subject") or {}).get("lookups", {}).get(table_key)
    if not isinstance(lookup, dict) or not lookup.get("steps"):
        raise PerformanceReportError(
            "当前 Skill 未配置分公司到法务、再到团队的确认映射链"
        )
    return lookup


def _field_value(row: dict[str, Any], field: _FieldRef) -> Any:
    if field.key in row:
        return row.get(field.key)
    raw = row.get("__raw__")
    if isinstance(raw, dict):
        return raw.get(field.source_name)
    return None


def _loss_metrics(
    rows: list[dict[str, Any]],
    fields: dict[str, _FieldRef],
    period: _ReportPeriod,
) -> dict[str, Any]:
    required_fields = (
        "opponent_type",
        "loss_reduction",
        "case_reason",
        "subject_amount",
        "interest",
        "total_principal",
        "total_appraisal",
        "total_litigation",
        "total_penalty",
    )
    missing = [field for field in required_fields if field not in fields]
    if missing:
        unavailable = {
            "status": "unavailable",
            "status_label": "规则字段未完整配置",
            "display": "暂不可计算",
        }
        return {
            "comprehensive_loss_rate": dict(unavailable),
            "substantial_loss_amount": dict(unavailable),
        }

    loss_rows = [_loss_row(row, fields) for row in rows]
    period_closed = [
        row
        for row in loss_rows
        if row.is_closed
        and row.close_date is not None
        and period.starts_on <= row.close_date <= period.ends_on
    ]
    payment_rows = [
        row
        for row in period_closed
        if any(
            value is not None
            for value in (
                row.total_principal,
                row.total_appraisal,
                row.total_litigation,
                row.total_penalty,
            )
        )
    ]
    claim_amount = sum(
        (
            (row.subject_amount or Decimal(0))
            + (row.interest or Decimal(0))
            for row in payment_rows
        ),
        Decimal(0),
    )
    payable_amount = sum(
        (
            (row.total_principal or Decimal(0))
            + (row.total_appraisal or Decimal(0))
            + (row.total_litigation or Decimal(0))
            + (row.total_penalty or Decimal(0))
            for row in payment_rows
        ),
        Decimal(0),
    )
    comprehensive_rate = (
        (claim_amount - payable_amount) / claim_amount * Decimal(100)
        if claim_amount
        else Decimal(0)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    year_start = period.ends_on.replace(month=1, day=1)
    substantial_rows = [
        row
        for row in loss_rows
        if row.is_closed
        and row.close_date is not None
        and year_start <= row.close_date <= period.ends_on
        and row.opponent_type in {"供应商", "班组"}
        and row.case_reason == "无争议-债权债务明确"
        and row.loss_reduction is not None
        and row.loss_reduction > 0
    ]
    substantial_yuan = sum(
        (row.loss_reduction or Decimal(0) for row in substantial_rows),
        Decimal(0),
    )
    substantial_wan = (substantial_yuan / Decimal(10000)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return {
        "comprehensive_loss_rate": {
            "status": "calculated",
            "value": f"{comprehensive_rate:.2f}",
            "display": f"{comprehensive_rate:.2f}%",
            "eligible_case_count": len(payment_rows),
            "claim_amount": _decimal_text(claim_amount),
            "payable_amount": _decimal_text(payable_amount),
        },
        "substantial_loss_amount": {
            "status": "calculated",
            "value_yuan": _decimal_text(substantial_yuan),
            "value_wan": f"{substantial_wan:.2f}",
            "display": f"{substantial_wan:.2f}万元",
            "eligible_case_count": len(substantial_rows),
        },
    }


def _weekly_loss_not_applicable() -> dict[str, Any]:
    not_applicable = {
        "status": "not_applicable",
        "status_label": "当前 Skill 未配置周维度减损口径",
        "display": "仅支持月维度",
    }
    return {
        "comprehensive_loss_rate": dict(not_applicable),
        "substantial_loss_amount": dict(not_applicable),
    }


def _loss_row(
    row: dict[str, Any],
    fields: dict[str, _FieldRef],
) -> _LossRow:
    return _LossRow(
        is_closed=_is_closed(_field_value(row, fields["is_closed"])),
        close_date=_optional_date(_field_value(row, fields["close_date"])),
        opponent_type=str(
            _field_value(row, fields["opponent_type"]) or ""
        ).strip(),
        loss_reduction=_optional_decimal(
            _field_value(row, fields["loss_reduction"])
        ),
        case_reason=str(_field_value(row, fields["case_reason"]) or "").strip(),
        subject_amount=_optional_decimal(
            _field_value(row, fields["subject_amount"])
        ),
        interest=_optional_decimal(_field_value(row, fields["interest"])),
        total_principal=_optional_decimal(
            _field_value(row, fields["total_principal"])
        ),
        total_appraisal=_optional_decimal(
            _field_value(row, fields["total_appraisal"])
        ),
        total_litigation=_optional_decimal(
            _field_value(row, fields["total_litigation"])
        ),
        total_penalty=_optional_decimal(
            _field_value(row, fields["total_penalty"])
        ),
    )


def _scope_report(
    scope_key: str,
    scope_name: str,
    cases: list[_CaseRow],
    period: _ReportPeriod,
    targets: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    scope_type: Literal["overall", "team", "person"],
    data_incomplete: bool,
    loss_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_stock = _stock_as_of(cases, period.ends_on)
    previous_stock = _stock_as_of(cases, period.previous_cutoff)
    last_year_stock = _stock_as_of(cases, period.last_year_cutoff)
    year_start = period.ends_on.replace(month=1, day=1)
    last_year_start = period.last_year_cutoff.replace(month=1, day=1)
    year_to_date_new = _new_between(cases, year_start, period.ends_on)
    last_year_to_date_new = _new_between(
        cases,
        last_year_start,
        period.last_year_cutoff,
    )
    period_new = _new_between(cases, period.starts_on, period.ends_on)
    period_closed = _closed_between(cases, period.starts_on, period.ends_on)
    stock_yoy_value = _change_rate(len(current_stock), len(last_year_stock))
    stock_period_value = _change_rate(len(current_stock), len(previous_stock))
    new_yoy_value = _change_rate(
        len(year_to_date_new),
        len(last_year_to_date_new),
    )
    branches = sorted({item.branch_name for item in cases if item.branch_name})
    branch_rows = [
        _branch_report(
            branch, [item for item in cases if item.branch_name == branch], period
        )
        for branch in branches
    ]
    branch_rows.sort(
        key=lambda item: (
            -int(item["stock_count"]),
            -int(item["year_to_date_new_count"]),
            str(item["branch_name"]),
        )
    )
    result = {
        "scope_key": scope_key,
        "scope_name": scope_name,
        "scope_type": scope_type,
        "stock_count": len(current_stock),
        "previous_stock_count": len(previous_stock),
        "last_year_stock_count": len(last_year_stock),
        "stock_yoy": _rate_result(stock_yoy_value),
        "stock_period_change": _rate_result(stock_period_value),
        "year_to_date_new_count": len(year_to_date_new),
        "last_year_to_date_new_count": len(last_year_to_date_new),
        "new_yoy": _rate_result(new_yoy_value),
        "period_new_count": len(period_new),
        "period_closed_count": len(period_closed),
        "stock_target": _target_result(
            "存量同比下降率",
            stock_yoy_value,
            targets,
            scope_key=scope_key,
            scope_name=scope_name,
            period=period,
            default_comparison="at_most",
            data_incomplete=data_incomplete,
        ),
        "new_target": _target_result(
            "新增同比下降率",
            new_yoy_value,
            targets,
            scope_key=scope_key,
            scope_name=scope_name,
            period=period,
            default_comparison="at_most",
            data_incomplete=data_incomplete,
        ),
        "branches": branch_rows,
        "period_new_cases": [_case_detail(item) for item in period_new],
        "period_closed_cases": [_case_detail(item) for item in period_closed],
    }
    if scope_type == "overall" and loss_metrics is not None:
        result["loss_metrics"] = copy.deepcopy(loss_metrics)
    return result


def _branch_report(
    branch_name: str,
    cases: list[_CaseRow],
    period: _ReportPeriod,
) -> dict[str, Any]:
    current_stock = _stock_as_of(cases, period.ends_on)
    previous_stock = _stock_as_of(cases, period.previous_cutoff)
    last_year_stock = _stock_as_of(cases, period.last_year_cutoff)
    year_to_date_new = _new_between(
        cases,
        period.ends_on.replace(month=1, day=1),
        period.ends_on,
    )
    last_year_to_date_new = _new_between(
        cases,
        period.last_year_cutoff.replace(month=1, day=1),
        period.last_year_cutoff,
    )
    return {
        "branch_name": branch_name,
        "lawyer_name": next(
            (item.lawyer_name for item in cases if item.lawyer_name),
            "",
        ),
        "stock_count": len(current_stock),
        "previous_stock_count": len(previous_stock),
        "last_year_stock_count": len(last_year_stock),
        "stock_yoy": _rate_result(
            _change_rate(len(current_stock), len(last_year_stock))
        ),
        "stock_period_change": _rate_result(
            _change_rate(len(current_stock), len(previous_stock))
        ),
        "year_to_date_new_count": len(year_to_date_new),
        "last_year_to_date_new_count": len(last_year_to_date_new),
        "new_yoy": _rate_result(
            _change_rate(len(year_to_date_new), len(last_year_to_date_new))
        ),
        "period_new_count": len(_new_between(cases, period.starts_on, period.ends_on)),
        "period_closed_count": len(
            _closed_between(cases, period.starts_on, period.ends_on)
        ),
    }


def _stock_as_of(cases: list[_CaseRow], cutoff: date) -> list[_CaseRow]:
    return [
        item
        for item in cases
        if item.register_date <= cutoff
        and (
            not item.is_closed
            or (item.close_date is not None and item.close_date > cutoff)
        )
    ]


def _new_between(
    cases: list[_CaseRow],
    starts_on: date,
    ends_on: date,
) -> list[_CaseRow]:
    return [item for item in cases if starts_on <= item.register_date <= ends_on]


def _closed_between(
    cases: list[_CaseRow],
    starts_on: date,
    ends_on: date,
) -> list[_CaseRow]:
    return [
        item
        for item in cases
        if item.is_closed
        and item.close_date is not None
        and starts_on <= item.close_date <= ends_on
    ]


def _case_detail(item: _CaseRow) -> dict[str, Any]:
    return {
        "branch_name": item.branch_name,
        "lawyer_name": item.lawyer_name,
        "case_name": item.case_name,
        "source_row_number": item.source_row_number,
    }


def _selected_scope(
    payload: dict[str, Any],
    scope_key: str,
) -> dict[str, Any]:
    for scope in payload.get("scopes") or ():
        if str(scope.get("scope_key") or "") == str(scope_key or ""):
            return dict(scope)
    raise PerformanceReportError("未找到要导出的团队或整体范围")


def _export_warning_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "type": "团队归属待确认",
            "source_row_number": item.get("source_row_number"),
            "subject": str(item.get("source_value") or "未识别值"),
            "message": str(item.get("message") or "无法确认团队归属"),
        }
        for item in payload.get("assignment_errors") or ()
        if isinstance(item, dict)
    ]
    rows.extend(
        {
            "type": "数据完整性问题",
            "source_row_number": item.get("source_row_number"),
            "subject": str(item.get("case_name") or "案件名称未识别"),
            "message": str(item.get("message") or "原始数据不完整"),
        }
        for item in payload.get("data_quality_errors") or ()
        if isinstance(item, dict)
    )
    return rows


def _arrow_rate(rate: dict[str, Any]) -> str:
    try:
        value = Decimal(str(rate.get("value") or "0"))
    except (InvalidOperation, ValueError):
        value = Decimal(0)
    arrow = "↑" if value > 0 else "↓"
    return f"{arrow}{abs(value):.2f}%"


def _sentence_rate(rate: dict[str, Any]) -> str:
    display = str(rate.get("display") or "").strip()
    for label in ("下降", "增长", "持平"):
        if display.startswith(label):
            return f"{label} {display[len(label) :]}"
    return display or "暂不可比"


def _style_export_sheet(sheet: Any) -> None:
    header_fill = PatternFill("solid", fgColor="4477C2")
    header_font = Font(color="FFFFFF", bold=True, size=12)
    total_font = Font(bold=True, size=11)
    border = Border(
        left=Side(style="thin", color="303030"),
        right=Side(style="thin", color="303030"),
        top=Side(style="thin", color="303030"),
        bottom=Side(style="thin", color="303030"),
    )
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in sheet.iter_rows():
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center")
    for cell in sheet[sheet.max_row]:
        cell.font = total_font
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index in range(1, sheet.max_column + 1):
        widest = max(
            len(str(sheet.cell(row=row, column=index).value or ""))
            for row in range(1, sheet.max_row + 1)
        )
        sheet.column_dimensions[
            sheet.cell(row=1, column=index).column_letter
        ].width = min(
            max(widest + 3, 13),
            36,
        )
    sheet.row_dimensions[1].height = 25


def _configure_document_styles(document: Document) -> None:
    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(10.5)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    for section in document.sections:
        section.top_margin = Pt(48)
        section.bottom_margin = Pt(48)
        section.left_margin = Pt(50)
        section.right_margin = Pt(50)


def _add_section_heading(document: Document, text: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(10)
    paragraph.paragraph_format.space_after = Pt(4)
    run = paragraph.add_run(text)
    run.bold = True
    run.font.size = Pt(12)


def _chinese_section_number(value: int) -> str:
    return {
        1: "一",
        2: "二",
        3: "三",
        4: "四",
        5: "五",
    }.get(value, str(value))


def _add_business_table(
    document: Document,
    headers: list[str],
    rows: list[list[Any]],
) -> None:
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = True
    for index, header in enumerate(headers):
        cell = table.rows[0].cells[index]
        cell.text = str(header)
        _shade_docx_cell(cell, "3566B4")
        for run in cell.paragraphs[0].runs:
            run.bold = True
            run.font.color.rgb = RGBColor(255, 255, 255)
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for index, value in enumerate(values):
            cells[index].text = str(value if value is not None else "")
            cells[index].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        if row_index == len(rows) - 1:
            for cell in cells:
                for run in cell.paragraphs[0].runs:
                    run.bold = True


def _shade_docx_cell(cell: Any, color: str) -> None:
    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:fill"), color)


def _change_rate(current: int, comparison: int) -> Decimal:
    if comparison == 0:
        return Decimal(0)
    return (
        (Decimal(current) - Decimal(comparison)) / Decimal(comparison) * Decimal(100)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _rate_result(value: Decimal) -> dict[str, str]:
    if value < 0:
        direction = "decline"
        label = "下降"
    elif value > 0:
        direction = "growth"
        label = "增长"
    else:
        direction = "flat"
        label = "持平"
    magnitude = abs(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "value": str(value),
        "direction": direction,
        "display": f"{label}{magnitude:.2f}%",
    }


def _target_result(
    metric_name: str,
    actual: Decimal,
    targets: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    scope_key: str,
    scope_name: str,
    period: _ReportPeriod,
    default_comparison: Literal["at_least", "at_most", "equal"],
    data_incomplete: bool,
) -> dict[str, str]:
    target = _matching_target(
        metric_name,
        targets,
        scope_key=scope_key,
        scope_name=scope_name,
        period=period,
    )
    if target is None:
        return _unknown_target()
    try:
        target_value = Decimal(
            str(target.get("target_value") or "").replace("%", "").strip()
        )
    except (InvalidOperation, ValueError):
        return _unknown_target()
    comparison = str(target.get("comparison") or default_comparison).strip()
    if comparison == "at_least":
        achieved = actual >= target_value
    elif comparison == "at_most":
        achieved = actual <= target_value
    elif comparison == "equal":
        achieved = actual == target_value
    else:
        return {
            **_unknown_target(),
            "status_label": "目标判断方向待确认",
            "target_value": str(target_value),
            "target_display": _rate_result(target_value)["display"],
        }
    result = {
        "status": "achieved" if achieved else "not_achieved",
        "status_label": "达到目标" if achieved else "未达到目标",
        "target_value": str(target_value),
        "target_display": _rate_result(target_value)["display"],
        "comparison": comparison,
        "comparison_label": {
            "at_least": "不低于",
            "at_most": "不高于",
            "equal": "等于",
        }[comparison],
        "target_scope": str(
            target.get("scope_name")
            or target.get("scope_key")
            or target.get("team_name")
            or ""
        ),
        "effective_period": str(target.get("effective_period") or ""),
    }
    if data_incomplete:
        result["status"] = "data_incomplete"
        result["status_label"] = "数据不完整，目标待确认"
    return result


def _unknown_target() -> dict[str, str]:
    return {
        "status": "unknown",
        "status_label": "目标待确认",
        "target_value": "",
        "target_display": "",
        "comparison": "",
        "comparison_label": "",
        "target_scope": "",
        "effective_period": "",
    }


def _matching_target(
    metric_name: str,
    targets: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    scope_key: str,
    scope_name: str,
    period: _ReportPeriod,
) -> dict[str, Any] | None:
    scope_candidates = {scope_key.casefold(), scope_name.casefold()}
    scoped: list[tuple[date, int, dict[str, Any]]] = []
    generic: list[tuple[date, int, dict[str, Any]]] = []
    for index, item in enumerate(targets):
        if str(item.get("metric_name") or "").strip() != metric_name:
            continue
        effective_date = _target_effective_date(item, period)
        if effective_date is None or effective_date > period.ends_on:
            continue
        target_scope = str(
            item.get("scope_name")
            or item.get("scope_key")
            or item.get("team_name")
            or ""
        ).strip()
        if target_scope and target_scope.casefold() in scope_candidates:
            scoped.append((effective_date, index, item))
        elif not target_scope:
            generic.append((effective_date, index, item))
    candidates = scoped or generic
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2] if candidates else None


def _target_effective_date(
    target: dict[str, Any],
    period: _ReportPeriod,
) -> date | None:
    configured = str(target.get("effective_period") or "").strip()
    if not configured:
        return date.min
    compact = re.sub(r"\s+", "", configured)
    exact_day = re.fullmatch(
        r"(\d{4})(?:年|[-./])(\d{1,2})(?:月|[-./])(\d{1,2})(?:日)?(?:目标)?",
        compact,
    )
    if exact_day:
        return _safe_date(
            int(exact_day.group(1)),
            int(exact_day.group(2)),
            int(exact_day.group(3)),
        )
    month_end = re.fullmatch(
        r"(\d{4})年(\d{1,2})月(?:底|末)(?:目标)?",
        compact,
    )
    if month_end:
        year, month = int(month_end.group(1)), int(month_end.group(2))
        if not 1 <= month <= 12:
            return None
        return date(year, month, monthrange(year, month)[1])
    month_start = re.fullmatch(
        r"(\d{4})(?:年|[-./])(\d{1,2})(?:月)?(?:目标)?",
        compact,
    )
    if month_start:
        return _safe_date(
            int(month_start.group(1)),
            int(month_start.group(2)),
            1,
        )
    if compact == re.sub(r"\s+", "", period.label):
        return period.starts_on
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _primary_lawyer_name(value: Any) -> str:
    """Follow the Skill's explicit personal rule for slash-separated contacts."""

    return str(value or "").replace("／", "/").split("/", 1)[0].strip()


def _case_name(row: dict[str, Any]) -> str:
    raw = row.get("__raw__")
    if isinstance(raw, dict):
        for key in ("案件名称", "案件名", "项目名称"):
            value = str(raw.get(key) or "").strip()
            if value:
                return value
    row_number = _row_number(row)
    return f"原表第{row_number}行（案件名称未识别）" if row_number else "案件名称未识别"


def _optional_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    text = str(value).strip()
    if not text:
        return None
    normalized = (
        text.replace(",", "")
        .replace("，", "")
        .replace("￥", "")
        .replace("¥", "")
        .strip()
    )
    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _excel_amount(value: Any) -> int | float | None:
    parsed = _optional_decimal(value)
    if parsed is None:
        return None
    if parsed == parsed.to_integral_value():
        return int(parsed)
    return float(parsed)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _is_closed(value: Any) -> bool:
    return str(value or "").strip() == "是"


def _row_number(row: dict[str, Any]) -> int | None:
    try:
        value = int(row.get("__row_number__"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _assignment_error_message(error: dict[str, Any]) -> str:
    qualifier = "唯一" if bool(error.get("ambiguous")) else ""
    return (
        f"无法通过“{error.get('step_name') or '团队归属'}”"
        f"{qualifier}匹配：{error.get('value') or '空值'}"
    )


def _shift_year(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)
