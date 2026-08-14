from __future__ import annotations

from dataclasses import dataclass

from app.schemas import StructuredDailyReport

STATUS_COLLECTING = "collecting"
STATUS_PENDING_CONFIRMATION = "pending_confirmation"
STATUS_COMPLETED = "completed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"

CONFIRMATION_NONE = "none"
CONFIRMATION_USER_CONFIRMED = "user_confirmed"
CONFIRMATION_AUTO_SUBMITTED_TIMEOUT = "auto_submitted_timeout"
CONFIRMATION_ADMIN_CONFIRMED = "admin_confirmed"

SECTION_LABELS = {
    "today_work": "今天做了什么",
    "problems": "遇到了什么问题或风险",
    "tomorrow_plan": "明天计划做什么",
}

FOLLOWUP_SECTION_LABELS = {
    "today_work": "今日工作完成情况",
    "problems": "问题/风险",
    "tomorrow_plan": "明日计划（明天计划）",
}


@dataclass(frozen=True)
class ReportState:
    section_status: dict[str, bool]
    completeness_score: float
    status: str
    missing_sections: list[str]
    current_slot: str | None
    should_ask_followup: bool
    ready_for_confirmation: bool


@dataclass(frozen=True)
class DailyReportCompleteness:
    section_status: dict[str, bool]
    completeness_score: float
    missing_sections: tuple[str, ...]
    ready_for_confirmation: bool
    draft_status: str


def assess_daily_report_completeness(
    *,
    today_work: object,
    problems: object,
    tomorrow_plan: object,
    section_status: dict | None = None,
    acknowledged_empty_fields: set[str] | frozenset[str] = frozenset(),
) -> DailyReportCompleteness:
    """Apply one deterministic completeness rule across writers and reminders."""

    prior = section_status or {}
    acknowledged_empty = {
        field_name: bool(
            prior.get(f"{field_name}_acknowledged_empty")
            or field_name in acknowledged_empty_fields
        )
        for field_name in SECTION_LABELS
    }
    filled = {
        "today_work": bool(today_work) or acknowledged_empty["today_work"],
        "problems": bool(problems) or acknowledged_empty["problems"],
        "tomorrow_plan": bool(tomorrow_plan)
        or acknowledged_empty["tomorrow_plan"],
    }
    missing_sections = tuple(
        field_name for field_name in SECTION_LABELS if not filled[field_name]
    )
    completeness_score = round(
        (0.34 if filled["today_work"] else 0)
        + (0.33 if filled["problems"] else 0)
        + (0.33 if filled["tomorrow_plan"] else 0),
        4,
    )
    ready_for_confirmation = not missing_sections
    normalized_section_status = {
        **filled,
        "problems_acknowledged_empty": acknowledged_empty["problems"],
    }
    normalized_section_status.update(
        {
            f"{field_name}_acknowledged_empty": True
            for field_name in ("today_work", "tomorrow_plan")
            if acknowledged_empty[field_name]
        }
    )
    return DailyReportCompleteness(
        section_status=normalized_section_status,
        completeness_score=completeness_score,
        missing_sections=missing_sections,
        ready_for_confirmation=ready_for_confirmation,
        draft_status=(
            STATUS_PENDING_CONFIRMATION
            if ready_for_confirmation
            else STATUS_COLLECTING
        ),
    )


def infer_report_state(
    *,
    existing_section_status: dict | None,
    merged_today_work: list[str],
    merged_problems: list[str],
    merged_tomorrow_plan: list[str],
    structured: StructuredDailyReport,
    raw_input: str,
) -> ReportState:
    previous = existing_section_status or {}
    problem_ack = bool(previous.get("problems_acknowledged_empty")) or _mentions_no_problem(raw_input)
    if (
        structured.completeness >= 1
        and merged_today_work
        and merged_tomorrow_plan
        and not merged_problems
    ):
        problem_ack = True
    completeness_status = dict(previous)
    completeness_status["problems_acknowledged_empty"] = problem_ack
    assessment = assess_daily_report_completeness(
        today_work=merged_today_work,
        problems=merged_problems,
        tomorrow_plan=merged_tomorrow_plan,
        section_status=completeness_status,
    )
    missing_sections = list(assessment.missing_sections)
    current_slot = missing_sections[0] if missing_sections else None

    return ReportState(
        section_status=assessment.section_status,
        completeness_score=assessment.completeness_score,
        status=assessment.draft_status,
        missing_sections=missing_sections,
        current_slot=current_slot,
        should_ask_followup=bool(missing_sections),
        ready_for_confirmation=assessment.ready_for_confirmation,
    )


def build_followup_message(
    missing_sections: list[str],
    *,
    acknowledged: list[str] | None = None,
    today_work: list[str] | None = None,
    problems: list[str] | None = None,
    tomorrow_plan: list[str] | None = None,
) -> str:
    acknowledged = [item for item in (acknowledged or []) if item]
    if not missing_sections:
        return "好的，这版内容我先记下了。"

    followup = _build_missing_sections_prompt(missing_sections)
    if today_work or problems or tomorrow_plan:
        lines: list[str] = []
        if acknowledged:
            lines.append("好的，" + "；".join(acknowledged) + "。")
            lines.append("")
        lines.append("目前已收到复盘：")
        if today_work:
            lines.append(f"{FOLLOWUP_SECTION_LABELS['today_work']}：{_display_section(today_work)}")
        if problems:
            lines.append(f"{FOLLOWUP_SECTION_LABELS['problems']}：{_display_section(problems)}")
        if tomorrow_plan:
            lines.append(f"{FOLLOWUP_SECTION_LABELS['tomorrow_plan']}：{_display_section(tomorrow_plan)}")
        lines.append("")
        lines.append(followup)
        return "\n".join(lines)

    prefix = "好的，" + "；".join(acknowledged) + "。" if acknowledged else ""
    return f"{prefix}{followup}"


def _build_missing_sections_prompt(missing_sections: list[str]) -> str:
    if len(missing_sections) == 1:
        label = FOLLOWUP_SECTION_LABELS[missing_sections[0]]
        return f"请继续补充最后一项：{label}。"
    labels = "、".join(FOLLOWUP_SECTION_LABELS[key] for key in missing_sections)
    return f"请继续补充：{labels}。"


def build_confirmation_message(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    quality_warning: str | None,
    meta_notes: list[str] | None = None,
    updated: bool = False,
) -> str:
    opening = "已更新，我重新整理如下：" if updated else "我整理好了，请确认一下："
    notes = f"\n补充说明/需关注事项：{_display_section(meta_notes)}" if meta_notes else ""
    warning = f"\n提示：{quality_warning}" if quality_warning else ""
    return (
        f"{opening}\n\n"
        f"今日工作：{_display_section(today_work)}\n"
        f"问题/风险：{_display_section(problems, empty_fallback='暂无明显问题')}\n"
        f"明日计划：{_display_section(tomorrow_plan)}"
        f"{notes}"
        f"{warning}\n\n"
        "没问题回复“确认”即可；需要修改可以直接说。若一段时间内未回复，系统将按以上内容自动提交。"
    )


def build_completed_message(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    updated: bool = False,
) -> str:
    prefix = "已更新今日复盘：" if updated else "复盘已提交："
    return (
        f"{prefix}\n\n"
        f"今日工作：{_display_section(today_work)}\n"
        f"问题/风险：{_display_section(problems, empty_fallback='暂无明显问题')}\n"
        f"明日计划：{_display_section(tomorrow_plan)}"
    )


def _display_section(values: list[str], *, empty_fallback: str = "未填写") -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    if not cleaned:
        return empty_fallback
    if len(cleaned) == 1:
        return cleaned[0] if "\n" not in cleaned[0] else "\n" + cleaned[0]
    return "\n" + "\n".join(_numbered_item(index, value) for index, value in enumerate(cleaned, start=1))


def _numbered_item(index: int, value: str) -> str:
    stripped = value.strip()
    if stripped.startswith(f"{index}. ") or stripped.startswith(f"{index}、"):
        return stripped
    return f"{index}. {stripped}"


def _mentions_no_problem(raw_input: str) -> bool:
    compact = "".join(raw_input.lower().split())
    phrases = [
        "没问题",
        "没啥问题",
        "没什么问题",
        "没有问题",
        "暂无问题",
        "无问题",
        "问题不大",
        "没风险",
        "没啥风险",
        "没什么风险",
        "没有风险",
        "暂无风险",
        "无风险",
        "noissue",
        "noproblem",
        "norisk",
    ]
    return any(phrase in compact for phrase in phrases)
