from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any

from app.models import DailyReport, User

STATUS_LABELS = {
    "completed": "已完成",
    "pending_confirmation": "待确认",
    "collecting": "填写中",
    "skipped": "已跳过",
    "cancelled": "已取消",
    "missing": "未开始",
}

FIELD_LABELS = {
    "today_work": "今日工作",
    "problems": "问题/风险",
    "tomorrow_plan": "明日计划",
}

SUSPICIOUS_PATTERNS = [
    "测试",
    "一二三",
    "鸡蛋饼",
    "手抓饼",
    "吃嘛",
    "炸鸡",
    "不告诉你",
    "你赶紧回家",
    "哈哈",
    "随便",
    "乱填",
    "瞎填",
]

VAGUE_PATTERNS = [
    r"^处理了?项目$",
    r"^跟进了?项目$",
    r"^处理了?问题$",
    r"^沟通了?问题$",
    r"^整理了?材料$",
    r"^恢复了?技能$",
    r"^完成了?工作$",
    r"^推进了?事项$",
]

LEGAL_RISK_KEYWORDS = [
    "诉讼费",
    "诉讼标的",
    "资料缺失",
    "原件",
    "无法找到",
    "讨薪",
    "堵门",
    "拉横幅",
    "开庭",
    "资产线索",
    "保全",
    "审计报告",
    "竣工报告",
    "调解",
]


def build_report_rows(
    users: list[User],
    reports: list[DailyReport],
    *,
    historical_reports: list[DailyReport] | None = None,
) -> list[dict[str, Any]]:
    reports_by_user = {report.user_id: report for report in reports}
    history_by_user: dict[Any, list[DailyReport]] = defaultdict(list)
    for report in historical_reports or []:
        history_by_user[report.user_id].append(report)
    for user_reports in history_by_user.values():
        user_reports.sort(key=lambda item: item.report_date, reverse=True)

    rows: list[dict[str, Any]] = []
    for user in users:
        report = reports_by_user.get(user.id)
        risks = analyze_report_risks(user, report, history_by_user.get(user.id, []))
        rows.append(
            {
                "user": user,
                "team": getattr(user, "team", None),
                "report": report,
                "status": getattr(report, "status", "missing") if report else "missing",
                "status_label": status_label(getattr(report, "status", "missing") if report else "missing"),
                "today_work": list(getattr(report, "today_work", []) or []),
                "problems": list(getattr(report, "problems", []) or []),
                "tomorrow_plan": list(getattr(report, "tomorrow_plan", []) or []),
                "confirmation_type": getattr(report, "confirmation_type", "") if report else "",
                "confirmed_by_user": bool(getattr(report, "confirmed_by_user", False)) if report else False,
                "quality_warning": getattr(report, "quality_warning", "") if report else "",
                "risks": risks,
                "risk_summary": "；".join(risk["label"] for risk in risks),
            }
        )
    return rows


def analyze_report_risks(user: User, report: DailyReport | None, history: list[DailyReport] | None = None) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    if report is None:
        return [_risk("未提交", "high", "当天没有日报记录")]

    status = getattr(report, "status", "")
    if status == "pending_confirmation":
        risks.append(_risk("待确认", "medium", "日报已整理但用户尚未确认"))
    elif status == "collecting":
        missing = _missing_fields(report)
        missing_text = "、".join(FIELD_LABELS[field] for field in missing) if missing else "未完成字段"
        risks.append(_risk("填写未完成", "high", f"仍缺少：{missing_text}"))
    elif status not in {"completed", "pending_confirmation", "collecting"}:
        risks.append(_risk("状态异常", "medium", f"当前状态：{status}"))

    if getattr(report, "confirmation_type", "") == "auto_submitted_timeout":
        risks.append(_risk("自动提交", "medium", "未由本人主动确认"))
    if status == "completed" and not bool(getattr(report, "confirmed_by_user", False)):
        risks.append(_risk("未主动确认", "low", "日报已完成但不是用户主动确认"))
    if getattr(report, "quality_warning", None):
        risks.append(_risk("质量提醒", "medium", str(report.quality_warning)))

    field_values = {
        "today_work": list(getattr(report, "today_work", []) or []),
        "problems": list(getattr(report, "problems", []) or []),
        "tomorrow_plan": list(getattr(report, "tomorrow_plan", []) or []),
    }
    for field, values in field_values.items():
        _append_content_risks(risks, field, values)

    if _has_real_legal_risk(field_values["problems"]):
        risks.append(_risk("实质法务风险", "high", "问题/风险中包含诉讼、资产、资料、稳定等风险信号"))

    history = history or []
    if _same_for_recent_days(field_values["today_work"], [item.today_work for item in history], days=2):
        risks.append(_risk("连续进展无变化", "medium", "今日工作与最近多天高度一致"))
    if _same_for_recent_days(field_values["tomorrow_plan"], [item.tomorrow_plan for item in history], days=2):
        risks.append(_risk("连续计划无变化", "medium", "明日计划与最近多天高度一致"))
    if _no_problem_for_recent_days(report, history, days=3):
        risks.append(_risk("连续暂无问题", "low", "连续多天无问题，建议团队负责人抽查真实性"))

    return _dedupe_risks(risks)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "未知")


def completion_stats(rows: list[dict[str, Any]]) -> dict[str, int]:
    stats = {
        "total": len(rows),
        "completed": 0,
        "pending_confirmation": 0,
        "collecting": 0,
        "missing": 0,
        "high_risk": 0,
        "risk_users": 0,
    }
    for row in rows:
        status = row["status"]
        if status in stats:
            stats[status] += 1
        risks = row["risks"]
        if risks:
            stats["risk_users"] += 1
        if any(risk["severity"] == "high" for risk in risks):
            stats["high_risk"] += 1
    return stats


def format_items(values: list[str], *, empty: str = "") -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    return "；".join(cleaned) if cleaned else empty


def _append_content_risks(risks: list[dict[str, str]], field: str, values: list[str]) -> None:
    label = FIELD_LABELS[field]
    for value in values:
        compact = _compact(value)
        if not compact:
            continue
        if len(compact) <= 6 and field != "problems":
            risks.append(_risk("填写过短", "medium", f"{label}内容过短：{value}"))
        if any(pattern in compact for pattern in SUSPICIOUS_PATTERNS):
            risks.append(_risk("疑似测试/瞎填", "high", f"{label}包含疑似测试或非工作内容：{value}"))
        if any(re.search(pattern, compact) for pattern in VAGUE_PATTERNS):
            risks.append(_risk("内容笼统", "medium", f"{label}缺少具体项目、案件或事项对象：{value}"))


def _missing_fields(report: DailyReport) -> list[str]:
    missing: list[str] = []
    if not getattr(report, "today_work", None):
        missing.append("today_work")
    section_status = getattr(report, "section_status", None) or {}
    problems_done = bool(getattr(report, "problems", None)) or bool(section_status.get("problems_acknowledged_empty"))
    if not problems_done:
        missing.append("problems")
    if not getattr(report, "tomorrow_plan", None):
        missing.append("tomorrow_plan")
    return missing


def _same_for_recent_days(current: list[str], historical_values: list[list[str]], *, days: int) -> bool:
    normalized_current = [_compact(item) for item in current if _compact(item)]
    if not normalized_current:
        return False
    checked = 0
    for values in historical_values:
        normalized = [_compact(item) for item in values if _compact(item)]
        if not normalized:
            continue
        checked += 1
        if normalized != normalized_current:
            return False
        if checked >= days:
            return True
    return False


def _no_problem_for_recent_days(report: DailyReport, history: list[DailyReport], *, days: int) -> bool:
    reports = [report, *history]
    checked = 0
    for item in reports:
        problems = list(getattr(item, "problems", []) or [])
        section_status = getattr(item, "section_status", None) or {}
        no_problem = bool(section_status.get("problems_acknowledged_empty")) or all(_is_no_problem_text(text) for text in problems)
        if not no_problem:
            return False
        checked += 1
        if checked >= days:
            return True
    return False


def _is_no_problem_text(text: str) -> bool:
    compact = _compact(text)
    return compact in {"暂无明显问题", "暂无问题", "无明显问题", "无问题", "没有问题", "没问题"}


def _has_real_legal_risk(values: list[str]) -> bool:
    compact = _compact("；".join(values))
    return any(_compact(keyword) in compact for keyword in LEGAL_RISK_KEYWORDS)


def _risk(label: str, severity: str, reason: str) -> dict[str, str]:
    return {"label": label, "severity": severity, "reason": reason}


def _dedupe_risks(risks: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for risk in risks:
        key = (risk["label"], risk["reason"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(risk)
    return deduped


def _compact(text: str) -> str:
    return re.sub(r"[\s，。,.、；;：:！!？?（）()【】\[\]\"'“”‘’]+", "", str(text).strip().lower())
