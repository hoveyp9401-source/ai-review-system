from __future__ import annotations

from datetime import date
from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.repositories import get_active_users, list_reports_for_date
from app.utils.time import today_in_timezone

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/reports", response_class=HTMLResponse)
async def reports_dashboard(
    report_date: date | None = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> str:
    target_date = report_date or today_in_timezone(settings.timezone)
    users = await get_active_users(session)
    reports = await list_reports_for_date(session, target_date)
    reports_by_user = {report.user_id: report for report in reports}

    total = len(users)
    completed = sum(1 for user in users if reports_by_user.get(user.id) and reports_by_user[user.id].status == "completed")
    pending = sum(1 for user in users if reports_by_user.get(user.id) and reports_by_user[user.id].status == "pending_confirmation")
    collecting = sum(1 for user in users if reports_by_user.get(user.id) and reports_by_user[user.id].status == "collecting")
    missing = total - sum(1 for user in users if user.id in reports_by_user)

    rows = []
    problem_items = []
    tomorrow_items = []
    for user in users:
        report = reports_by_user.get(user.id)
        if report is None:
            rows.append(_row(user.name, "未开始", "", "", "", "", "", ""))
            continue
        problems = _display_list(report.problems, empty="暂无明显问题" if report.section_status.get("problems_acknowledged_empty") else "")
        tomorrow_plan = _display_list(report.tomorrow_plan)
        rows.append(
            _row(
                user.name,
                report.status,
                _display_list(report.today_work),
                problems,
                tomorrow_plan,
                report.confirmation_type,
                "是" if report.confirmed_by_user else "否",
                report.quality_warning or "",
            )
        )
        if report.problems:
            problem_items.append(f"<li><strong>{escape(user.name)}</strong>：{escape(_display_list(report.problems))}</li>")
        if report.tomorrow_plan:
            tomorrow_items.append(f"<li><strong>{escape(user.name)}</strong>：{escape(tomorrow_plan)}</li>")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI 每日复盘看板</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; color: #1f2937; background: #f5f7fb; }}
    header {{ background: #ffffff; border-bottom: 1px solid #e5e7eb; padding: 20px 28px; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    main {{ padding: 24px 28px 40px; }}
    .meta {{ color: #6b7280; font-size: 14px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 12px; margin-bottom: 22px; }}
    .metric {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px; }}
    .metric b {{ display: block; font-size: 24px; margin-bottom: 4px; }}
    .panel {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 18px; }}
    h2 {{ font-size: 17px; margin: 0 0 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 8px; text-align: left; vertical-align: top; }}
    th {{ color: #374151; background: #f9fafb; font-weight: 700; }}
    td {{ line-height: 1.5; }}
    .status {{ white-space: nowrap; font-weight: 700; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ margin: 6px 0; }}
    .empty {{ color: #9ca3af; }}
    .actions {{ display: flex; gap: 10px; align-items: center; margin-top: 10px; }}
    input {{ border: 1px solid #d1d5db; border-radius: 6px; padding: 7px 9px; }}
    button {{ border: 0; border-radius: 6px; background: #2563eb; color: white; padding: 8px 12px; cursor: pointer; }}
  </style>
</head>
<body>
  <header>
    <h1>AI 每日复盘看板</h1>
    <div class="meta">日期：{target_date.isoformat()} · 内部测试版</div>
  </header>
  <main>
    <section class="metrics">
      {_metric("总人数", total)}
      {_metric("已完成", completed)}
      {_metric("待确认", pending)}
      {_metric("填写中", collecting)}
      {_metric("未开始", missing)}
    </section>
    <section class="panel">
      <h2>个人日报列表</h2>
      <table>
        <thead><tr><th>姓名</th><th>状态</th><th>今日工作</th><th>问题/困难</th><th>明日计划</th><th>确认方式</th><th>主动确认</th><th>质量提醒</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    <section class="panel">
      <h2>问题/风险汇总</h2>
      <ul>{''.join(problem_items) if problem_items else '<li class="empty">暂无已记录问题/风险</li>'}</ul>
    </section>
    <section class="panel">
      <h2>明日计划汇总</h2>
      <ul>{''.join(tomorrow_items) if tomorrow_items else '<li class="empty">暂无已记录明日计划</li>'}</ul>
    </section>
    <section class="panel">
      <h2>部门汇总生成入口</h2>
      <form class="actions" method="post" action="/tasks/summaries/{target_date.isoformat()}">
        <input type="text" value="{target_date.isoformat()}" aria-label="汇总日期" readonly />
        <button type="submit">生成部门汇总</button>
      </form>
    </section>
  </main>
</body>
</html>"""


def _display_list(values: list[str], *, empty: str = "") -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    return "；".join(cleaned) if cleaned else empty


def _metric(label: str, value: int) -> str:
    return f'<div class="metric"><b>{value}</b><span>{escape(label)}</span></div>'


def _row(
    name: str,
    status: str,
    today_work: str,
    problems: str,
    tomorrow_plan: str,
    confirmation_type: str,
    confirmed: str,
    quality_warning: str,
) -> str:
    cells = [
        escape(name),
        f'<span class="status">{escape(status)}</span>',
        escape(today_work),
        escape(problems),
        escape(tomorrow_plan),
        escape(confirmation_type),
        escape(confirmed),
        escape(quality_warning),
    ]
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
