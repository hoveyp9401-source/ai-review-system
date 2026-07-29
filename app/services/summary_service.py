from __future__ import annotations

import uuid
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.llm.extractor import TeamSummaryGenerator
from app.models import DailyReport
from app.repositories import get_active_teams, get_active_users, list_reports_between_dates, list_reports_for_date, upsert_team_summary
from app.services.management_daily_briefing import ManagementDailyBriefingService
from app.services.report_risk import build_report_rows, completion_stats, format_items
from app.utils.time import now_in_timezone

TEAM_LEADER_ROLES = {"team_lead", "team_leader", "team_manager", "leader", "manager", "负责人", "团队负责人"}
DEPARTMENT_HEAD_ROLES = {"department_head", "dept_head", "department_manager", "admin", "部门负责人", "部长"}
TEAM_BRIEFING_CC_NAMES = {"赵卫中"}
LEGAL_TEAM_NAMES = {"\u6cd5\u52a1\u4e94\u90e8"}
LEGAL_ATTENTION_KEYWORDS = (
    "\u6848\u4ef6",
    "\u8bc9\u8bbc",
    "\u6cd5\u9662",
    "\u5f00\u5ead",
    "\u8d77\u8bc9",
    "\u4e0a\u8bc9",
    "\u4ef2\u88c1",
    "\u5408\u540c",
    "\u534f\u8bae",
    "\u51fd",
    "\u7528\u5370",
    "\u6750\u6599",
    "\u8bc1\u636e",
    "\u8d44\u6599",
    "\u8d39\u7528",
    "\u8d23\u4efb",
    "\u91d1\u989d",
    "\u98ce\u9669",
    "\u5361\u4f4f",
    "\u56de\u590d",
    "\u5ba1\u6838",
    "\u4fee\u6539",
    "\u8c08\u5224",
)


class SummaryService:
    def __init__(self, settings: Settings, generator: TeamSummaryGenerator):
        self.settings = settings
        self.generator = generator

    async def generate_for_date(self, session: AsyncSession, summary_date: date) -> list[dict[str, Any]]:
        generated: list[dict[str, Any]] = []
        teams = await get_active_teams(session)
        all_reports = await list_reports_for_date(session, summary_date)

        for team in teams:
            team_reports = [report for report in all_reports if report.team_id == team.id]
            generated.append(await self._generate_one(session, "team", team.id, summary_date, team_reports))

        generated.append(await self._generate_one(session, "department", None, summary_date, all_reports))
        return generated

    async def build_daily_briefings(self, session: AsyncSession, report_date: date) -> dict[str, Any]:
        if str(
            getattr(
                self.settings,
                "legal_daily_dashboard_tenant_id",
                "",
            )
            or ""
        ).strip():
            return await ManagementDailyBriefingService(self.settings).build(
                session,
                report_date,
            )

        users = await get_active_users(session)
        teams = await get_active_teams(session)
        reports = await list_reports_for_date(session, report_date)
        history = await list_reports_between_dates(session, report_date - timedelta(days=3), report_date - timedelta(days=1))
        rows = build_report_rows(users, reports, historical_reports=history)
        rows_by_team: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            rows_by_team[row["user"].team_id].append(row)

        team_messages = []
        team_detail_messages = []
        for team in teams:
            team_rows = rows_by_team.get(team.id, [])
            recipients = _team_briefing_recipients(team_rows, users, team)
            team_messages.append(
                {
                    "scope": "team",
                    "team_id": str(team.id),
                    "team_name": team.name,
                    "recipients": _serialize_users(recipients),
                    "target_count": len(recipients),
                    "stats": completion_stats(team_rows),
                    "text": build_team_briefing_text(team.name, report_date, team_rows),
                }
            )
            team_detail_messages.append(
                {
                    "scope": "team_detail",
                    "team_id": str(team.id),
                    "team_name": team.name,
                    "recipients": _serialize_users(recipients),
                    "target_count": len(recipients),
                    "stats": completion_stats(team_rows),
                    "text": build_team_detail_briefing_text(team.name, report_date, team_rows),
                }
            )

        department_recipients = _department_heads(users)
        department_message = {
            "scope": "department",
            "department_name": teams[0].department_name if teams else "法务部",
            "recipients": _serialize_users(department_recipients),
            "target_count": len(department_recipients),
            "stats": completion_stats(rows),
            "text": build_department_briefing_text(report_date, rows, team_messages),
        }
        department_detail_message = {
            "scope": "department_detail",
            "department_name": teams[0].department_name if teams else "\u6cd5\u52a1\u90e8",
            "recipients": _serialize_users(department_recipients),
            "target_count": len(department_recipients),
            "stats": completion_stats(rows),
            "text": build_department_detail_briefing_text(report_date, rows),
        }
        return {
            "date": report_date.isoformat(),
            "team_messages": team_messages,
            "team_detail_messages": team_detail_messages,
            "department_message": department_message,
            "department_detail_message": department_detail_message,
        }

    async def _generate_one(
        self,
        session: AsyncSession,
        scope: str,
        team_id: uuid.UUID | None,
        summary_date: date,
        reports: list[DailyReport],
    ) -> dict[str, Any]:
        report_payloads = [serialize_report_for_summary(report) for report in reports]
        summary_payload = await self.generator.generate(report_payloads) if report_payloads else empty_summary()
        complete_count = sum(1 for report in reports if report.status == "completed")
        generated_at = now_in_timezone(self.settings.timezone)
        summary = await upsert_team_summary(
            session,
            scope=scope,
            team_id=team_id,
            summary_date=summary_date,
            key_work=summary_payload.key_work,
            major_problems=summary_payload.major_problems,
            risks=summary_payload.risks,
            tomorrow_plan_distribution=summary_payload.tomorrow_plan_distribution,
            raw_summary=summary_payload.model_dump(),
            report_count=len(reports),
            complete_count=complete_count,
            llm_model=self.generator.client.model,
            generated_at=generated_at,
        )
        return {
            "id": str(summary.id),
            "scope": scope,
            "team_id": str(team_id) if team_id else None,
            "report_count": len(reports),
            "complete_count": complete_count,
        }


def serialize_report_for_summary(report: DailyReport) -> dict[str, Any]:
    return {
        "user_id": str(report.user_id),
        "team_id": str(report.team_id),
        "today_work": report.today_work,
        "problems": report.problems,
        "tomorrow_plan": report.tomorrow_plan,
        "emotion": report.emotion,
        "status": report.status,
    }


def empty_summary():
    from app.schemas import TeamSummaryPayload

    return TeamSummaryPayload()


def _build_team_briefing_text_legacy(team_name: str, report_date: date, rows: list[dict[str, Any]]) -> str:
    stats = completion_stats(rows)
    risk_rows = [row for row in rows if row["risks"]]
    completed_lines = []
    for row in rows:
        completed_lines.append(
            f"- {row['user'].name}：{row['status_label']}；风险：{row['risk_summary'] or '无'}"
        )
    key_work = _section_lines(rows, "today_work", limit=8)
    problem_lines = _section_lines(rows, "problems", limit=8, empty_placeholder=False)
    tomorrow_lines = _section_lines(rows, "tomorrow_plan", limit=8)
    risk_lines = _risk_lines(risk_rows, limit=10)
    return "\n".join(
        [
            f"【{team_name}】每日复盘汇总 {report_date.isoformat()}",
            "",
            f"完成情况：应填 {stats['total']} 人，已完成 {stats['completed']} 人，待确认 {stats['pending_confirmation']} 人，填写中 {stats['collecting']} 人，未开始 {stats['missing']} 人。",
            f"风险概览：有风险 {stats['risk_users']} 人，其中高风险 {stats['high_risk']} 人。",
            "",
            "今日重点工作：",
            *(key_work or ["- 暂无已提交内容"]),
            "",
            "问题/风险：",
            *(problem_lines or ["- 暂无已记录问题/风险"]),
            "",
            "明日计划：",
            *(tomorrow_lines or ["- 暂无已记录明日计划"]),
            "",
            "风险标注：",
            *(risk_lines or ["- 暂无风险标签"]),
            "",
            "成员明细：",
            *(completed_lines or ["- 暂无成员"]),
        ]
    )


def _build_department_briefing_text_legacy(report_date: date, rows: list[dict[str, Any]], team_messages: list[dict[str, Any]]) -> str:
    stats = completion_stats(rows)
    team_lines = []
    for item in team_messages:
        team_stats = item["stats"]
        team_lines.append(
            f"- {item['team_name']}：应填 {team_stats['total']}，已完成 {team_stats['completed']}，待确认 {team_stats['pending_confirmation']}，填写中 {team_stats['collecting']}，未开始 {team_stats['missing']}，高风险 {team_stats['high_risk']}"
        )
    risk_lines = _risk_lines([row for row in rows if row["risks"]], limit=20)
    member_lines = [
        f"- {getattr(row['team'], 'name', '')} / {row['user'].name}：{row['status_label']}；风险：{row['risk_summary'] or '无'}"
        for row in rows
    ]
    return "\n".join(
        [
            f"【法务部】AI 每日复盘总览 {report_date.isoformat()}",
            "",
            f"整体完成情况：应填 {stats['total']} 人，已完成 {stats['completed']} 人，待确认 {stats['pending_confirmation']} 人，填写中 {stats['collecting']} 人，未开始 {stats['missing']} 人。",
            f"整体风险：有风险 {stats['risk_users']} 人，其中高风险 {stats['high_risk']} 人。",
            "",
            "各团队情况：",
            *(team_lines or ["- 暂无团队数据"]),
            "",
            "重点风险：",
            *(risk_lines or ["- 暂无风险标签"]),
            "",
            "个人明细：",
            *(member_lines or ["- 暂无成员数据"]),
        ]
    )


def build_team_briefing_text(team_name: str, report_date: date, rows: list[dict[str, Any]]) -> str:
    if _is_legal_team(team_name):
        return build_legal_team_briefing_text(team_name, report_date, rows)

    stats = completion_stats(rows)
    attention_lines = _attention_lines(rows, limit=12)
    plan_lines = _key_section_lines(rows, "tomorrow_plan", limit=12)
    return "\n".join(
        [
            f"**【{team_name}】晨报总览｜{report_date.isoformat()}**",
            _progress_line(stats),
            "",
            "**一、填报情况**",
            _completion_line(stats),
            _risk_count_line(stats),
            "",
            "**二、风险/问题/卡点**",
            *(attention_lines or ["- 暂无需要在总览中特别提示的风险或卡点"]),
            "",
            "**三、明日关键计划**",
            *(plan_lines or ["- 暂无需要在总览中特别提示的明日关键计划"]),
            "",
            "全员明细保留在日报明细中；普通用印、邮寄、登记、归档等基础事项不在总览展开。",
        ]
    )


def build_legal_team_briefing_text(team_name: str, report_date: date, rows: list[dict[str, Any]]) -> str:
    stats = completion_stats(rows)
    key_work_lines = _legal_key_work_lines(rows, limit=12)
    attention_lines = _legal_attention_lines(rows, limit=12)
    plan_lines = _legal_plan_lines(rows, limit=12)
    return "\n".join(
        [
            f"**【{team_name}】法务晨报总览｜{report_date.isoformat()}**",
            _progress_line(stats),
            "",
            "**一、填报进度**",
            _completion_line(stats),
            _risk_count_line(stats),
            "",
            "**二、重点法务事项**",
            *(key_work_lines or ["- 暂无需要在总览中特别提示的案件、合同、函件或资料事项"]),
            "",
            "**三、风险/卡点/需协调**",
            *(attention_lines or ["- 暂无需要负责人特别关注的风险、卡点或协调事项"]),
            "",
            "**四、今日跟进计划**",
            *(plan_lines or ["- 暂无需要在总览中特别提示的今日跟进计划"]),
            "",
            "全员明细会单独发送；总览优先呈现案件/争议、合同协议、函件资料、风险卡点和需负责人关注事项。",
        ]
    )


def build_team_detail_briefing_text(team_name: str, report_date: date, rows: list[dict[str, Any]]) -> str:
    detail_lines = _member_report_lines(rows, limit=80)
    return "\n".join(
        [
            f"**\u3010{team_name}\u3011\u5168\u5458\u65e5\u62a5\u660e\u7ec6\uff5c{report_date.isoformat()}**",
            "",
            *(detail_lines or ["- \u6682\u65e0\u6210\u5458\u65e5\u62a5\u660e\u7ec6"]),
        ]
    )


def build_department_briefing_text(report_date: date, rows: list[dict[str, Any]], team_messages: list[dict[str, Any]]) -> str:
    stats = completion_stats(rows)
    team_lines = []
    for item in team_messages:
        team_stats = item["stats"]
        team_lines.append(
            f"- **{item['team_name']}**：已完成 {team_stats['completed']}/{team_stats['total']}，未开始 {team_stats['missing']}，高风险 {team_stats['high_risk']}"
        )
    attention_lines = _attention_lines(rows, limit=18)
    plan_lines = _key_section_lines(rows, "tomorrow_plan", limit=18)
    return "\n".join(
        [
            f"**【部门】晨报总览｜{report_date.isoformat()}**",
            _progress_line(stats),
            "",
            "**一、填报情况**",
            _completion_line(stats),
            _risk_count_line(stats),
            "",
            "**二、各团队情况**",
            *(team_lines or ["- 暂无团队数据"]),
            "",
            "**三、风险/问题/卡点**",
            *(attention_lines or ["- 暂无需要在总览中特别提示的风险或卡点"]),
            "",
            "**四、明日关键计划**",
            *(plan_lines or ["- 暂无需要在总览中特别提示的明日关键计划"]),
        ]
    )


def build_department_detail_briefing_text(report_date: date, rows: list[dict[str, Any]]) -> str:
    detail_lines = _member_report_lines(rows, limit=200, include_team=True)
    return "\n".join(
        [
            f"**\u3010\u90e8\u95e8\u5168\u5458\u65e5\u62a5\u660e\u7ec6\u3011\uff5c{report_date.isoformat()}**",
            "",
            *(detail_lines or ["- \u6682\u65e0\u6210\u5458\u65e5\u62a5\u660e\u7ec6"]),
        ]
    )


def _member_report_lines(rows: list[dict[str, Any]], *, limit: int, include_team: bool = False) -> list[str]:
    lines: list[str] = []
    for row in rows:
        report = row["report"]
        problems_empty = bool(report and (report.section_status or {}).get("problems_acknowledged_empty"))
        problems = format_items(row["problems"], empty="暂无明显问题" if problems_empty else "未填写")
        lines.append(
            f"- {row['user'].name}：{row['status_label']}；"
            f"昨日工作：{format_items(row['today_work'], empty='未填写')}；"
            f"问题/风险：{problems}；"
            f"今日计划：{format_items(row['tomorrow_plan'], empty='未填写')}"
        )
        if len(lines) >= limit:
            break
    return lines


def _progress_line(stats: dict[str, int]) -> str:
    total = max(stats["total"], 1)
    ratio = stats["completed"] / total
    filled = max(0, min(10, round(ratio * 10)))
    bar = "█" * filled + "░" * (10 - filled)
    percent = round(ratio * 100)
    return f"进度：{bar} {percent}%（{stats['completed']}/{stats['total']}）"


def _completion_line(stats: dict[str, int]) -> str:
    return (
        f"已完成 {stats['completed']}｜待确认 {stats['pending_confirmation']}｜"
        f"填写中 {stats['collecting']}｜未开始 {stats['missing']}"
    )


def _risk_count_line(stats: dict[str, int]) -> str:
    return f"涉及风险 {stats['risk_users']} 人｜高风险 {stats['high_risk']} 人"


def _attention_lines(rows: list[dict[str, Any]], *, limit: int) -> list[str]:
    lines: list[str] = []
    for row in rows:
        for risk in row["risks"]:
            if not _risk_should_show_in_overview(risk):
                continue
            lines.append(f"- {row['user'].name}：[{risk['severity']}] {risk['label']} - {risk['reason']}")
            if len(lines) >= limit:
                return lines
        for problem in row["problems"]:
            if not _item_should_show_in_overview(problem, field="problems"):
                continue
            lines.append(f"- {row['user'].name}：{problem}")
            if len(lines) >= limit:
                return lines
    return lines


def _key_section_lines(rows: list[dict[str, Any]], field: str, *, limit: int) -> list[str]:
    lines: list[str] = []
    for row in rows:
        values = [
            value
            for value in row[field]
            if _item_should_show_in_overview(value, field=field)
        ]
        if not values:
            continue
        lines.append(f"- **{row['user'].name}**：{format_items(values)}")
        if len(lines) >= limit:
            break
    return lines


def _is_legal_team(team_name: str) -> bool:
    return _overview_compact(team_name) in {_overview_compact(name) for name in LEGAL_TEAM_NAMES}


def _legal_key_work_lines(rows: list[dict[str, Any]], *, limit: int) -> list[str]:
    return _legal_section_lines(rows, "today_work", limit=limit)


def _legal_plan_lines(rows: list[dict[str, Any]], *, limit: int) -> list[str]:
    return _legal_section_lines(rows, "tomorrow_plan", limit=limit)


def _legal_attention_lines(rows: list[dict[str, Any]], *, limit: int) -> list[str]:
    lines: list[str] = []
    for row in rows:
        for risk in row["risks"]:
            if not _risk_should_show_in_overview(risk):
                continue
            lines.append(f"- {row['user'].name}：[{risk['severity']}] {risk['label']} - {risk['reason']}")
            if len(lines) >= limit:
                return lines
        for problem in row["problems"]:
            compact = _overview_compact(problem)
            if compact in {_overview_compact(item) for item in NO_PROBLEM_TEXTS}:
                continue
            if not (_is_legal_attention_item(problem) or _item_should_show_in_overview(problem, field="problems")):
                continue
            lines.append(f"- {row['user'].name}：{problem}")
            if len(lines) >= limit:
                return lines
    return lines


def _legal_section_lines(rows: list[dict[str, Any]], field: str, *, limit: int) -> list[str]:
    lines: list[str] = []
    for row in rows:
        values = [value for value in row[field] if _is_legal_attention_item(value)]
        if not values:
            continue
        lines.append(f"- **{row['user'].name}**：{format_items(values)}")
        if len(lines) >= limit:
            break
    return lines


def _is_legal_attention_item(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = _overview_compact(text)
    legal_keywords = {_overview_compact(keyword) for keyword in LEGAL_ATTENTION_KEYWORDS}
    return any(keyword in compact for keyword in legal_keywords) or _has_attention_signal(text)


NO_PROBLEM_TEXTS = {
    "暂无明显问题",
    "暂无问题",
    "无明显问题",
    "无问题",
    "没有问题",
    "没问题",
}

ROUTINE_KEYWORDS = (
    "日常用印",
    "用印审核",
    "资料审核",
    "合同登记",
    "协议登记",
    "合同归档",
    "协议归档",
    "归档合同",
    "邮寄",
    "扫描",
    "值班",
    "流程审批",
    "日常工作",
    "继续今日工作",
)

ATTENTION_KEYWORDS = (
    "风险",
    "问题",
    "异常",
    "影响",
    "卡",
    "缺",
    "未",
    "待",
    "必须",
    "务必",
    "需要",
    "需",
    "跟进",
    "催",
    "闭环",
    "沟通",
    "协调",
    "确认",
    "会议",
    "开庭",
    "法院",
    "诉讼",
    "上诉",
    "系统",
    "费用",
    "重复",
    "错误",
    "VPN",
    "ERP",
)

NON_OVERVIEW_RISK_LABELS = {
    "填写过短",
    "内容笼统",
    "未主动确认",
    "连续暂无问题",
}


def _risk_should_show_in_overview(risk: dict[str, str]) -> bool:
    label = str(risk.get("label", "")).strip()
    severity = str(risk.get("severity", "")).strip()
    reason = str(risk.get("reason", "")).strip()
    if label in NON_OVERVIEW_RISK_LABELS:
        return False
    if severity == "high":
        return True
    return _item_should_show_in_overview(f"{label}{reason}", field="problems")


def _item_should_show_in_overview(value: str, *, field: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = _overview_compact(text)
    if compact in {_overview_compact(item) for item in NO_PROBLEM_TEXTS}:
        return False
    if _is_routine_overview_noise(text):
        return False
    if field == "tomorrow_plan":
        return _has_attention_signal(text)
    if field == "problems":
        return True
    return _has_attention_signal(text)


def _is_routine_overview_noise(text: str) -> bool:
    compact = _overview_compact(text)
    if not compact:
        return True
    if compact in {"日常工作", "继续今日工作", "继续日常工作"}:
        return True
    has_routine = any(_overview_compact(keyword) in compact for keyword in ROUTINE_KEYWORDS)
    if not has_routine:
        return False
    return not _has_attention_signal(text)


def _has_attention_signal(text: str) -> bool:
    compact = _overview_compact(text)
    return any(_overview_compact(keyword) in compact for keyword in ATTENTION_KEYWORDS)


def _overview_compact(text: str) -> str:
    return re.sub(r"[\s，。、“”‘’；;：:,.!?！？（）()\[\]【】\-_/]+", "", str(text or "").lower())


def _team_leaders(rows: list[dict[str, Any]]) -> list:
    return [row["user"] for row in rows if str(row["user"].role).lower() in TEAM_LEADER_ROLES]


def _team_briefing_recipients(rows: list[dict[str, Any]], all_users: list | None = None, team: Any | None = None) -> list:
    recipients = _team_leaders(rows)
    seen = {str(getattr(user, "id", "") or "") for user in recipients}
    candidates = [row["user"] for row in rows]
    team_code = str(getattr(team, "code", "") or "")
    team_name = str(getattr(team, "name", "") or "")
    if all_users and (team_code == "team-05" or team_name == "法务五部"):
        candidates.extend(all_users)
    for user in candidates:
        if user.name not in TEAM_BRIEFING_CC_NAMES:
            continue
        key = str(getattr(user, "id", "") or "")
        if key and key not in seen:
            recipients.append(user)
            seen.add(key)
    return recipients


def _department_heads(users: list) -> list:
    return [user for user in users if str(user.role).lower() in DEPARTMENT_HEAD_ROLES]


def _serialize_users(users: list) -> list[dict[str, str]]:
    return [
        {
            "id": str(user.id),
            "name": user.name,
            "dingtalk_user_id": user.dingtalk_user_id,
            "role": user.role,
        }
        for user in users
    ]


def _section_lines(rows: list[dict[str, Any]], field: str, *, limit: int, empty_placeholder: bool = True) -> list[str]:
    lines: list[str] = []
    for row in rows:
        values = row[field]
        if not values and not empty_placeholder:
            continue
        text = format_items(values)
        if not text:
            continue
        lines.append(f"- {row['user'].name}：{text}")
        if len(lines) >= limit:
            break
    return lines


def _risk_lines(rows: list[dict[str, Any]], *, limit: int) -> list[str]:
    lines: list[str] = []
    for row in rows:
        for risk in row["risks"]:
            lines.append(f"- {getattr(row['team'], 'name', '')} / {row['user'].name}：[{risk['severity']}] {risk['label']} - {risk['reason']}")
            if len(lines) >= limit:
                return lines
    return lines
