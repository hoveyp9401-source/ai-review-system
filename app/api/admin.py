from __future__ import annotations

import csv
import hmac
import io
import re
import uuid
from datetime import date, timedelta
from html import escape
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import ReportInteractionEvent, User, UserHabit
from app.repositories import get_active_teams, get_active_users, list_reports_between_dates, list_reports_for_date, set_user_habit_status
from app.services.dingtalk import DingTalkRobotClient
from app.services.report_risk import build_report_rows, completion_stats, format_items
from app.utils.time import today_in_timezone

ADMIN_SESSION_COOKIE = "ai_review_admin_token"
ADMIN_SESSION_MAX_AGE_SECONDS = 12 * 60 * 60


def evaluate_admin_auth(request: Request, settings: Settings) -> tuple[bool, int, str, bool]:
    if not settings.admin_enabled:
        return False, 404, "Not found", False
    expected_token = (settings.admin_token or "").strip()
    if not expected_token:
        return False, 403, "Admin access disabled", False
    header_token = (request.headers.get("X-Admin-Token") or "").strip()
    query_token = (request.query_params.get("admin_token") or "").strip()
    cookie_token = (request.cookies.get(ADMIN_SESSION_COOKIE) or "").strip()
    provided_token = header_token or query_token or cookie_token
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        return False, 403, "Forbidden", False
    return True, 200, "", bool(header_token or query_token)


def set_admin_session_cookie(response: Response, settings: Settings) -> None:
    expected_token = (settings.admin_token or "").strip()
    if not expected_token:
        return
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        expected_token,
        max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        path="/admin",
    )


def require_admin(request: Request, settings: Settings = Depends(get_settings)) -> None:
    ok, status_code, detail, _should_set_cookie = evaluate_admin_auth(request, settings)
    if not ok:
        raise HTTPException(status_code=status_code, detail=detail)


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

DEFAULT_TEST_MESSAGE = "【测试提醒】这是 AI 复盘助手的单人测试消息。收到后无需填写日报。"


@router.get("/reports", response_class=HTMLResponse)
async def reports_dashboard(
    report_date: date | None = None,
    team_id: str = "all",
    status: str = "all",
    risk: str = "all",
    notice: str = "",
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> str:
    target_date = report_date or today_in_timezone(settings.timezone)
    teams, rows = await _load_dashboard_rows(session, target_date, team_id=team_id)
    filtered_rows = _filter_rows(rows, status=status, risk=risk)
    stats = completion_stats(filtered_rows)
    export_url = "/admin/reports.csv?" + urlencode(
        {
            "report_date": target_date.isoformat(),
            "team_id": team_id,
            "status": status,
            "risk": risk,
        }
    )
    briefing_url = f"/tasks/daily-briefings/{target_date.isoformat()}"
    learning_url = f"/admin/learning?report_date={target_date.isoformat()}"

    risk_items = _risk_summary_items(filtered_rows)
    rows_html = "".join(_table_row(row) for row in filtered_rows) or '<tr><td colspan="11" class="empty">暂无匹配数据</td></tr>'
    notice_html = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>法务日报管理台</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; color: #1f2937; background: #f4f6f8; }}
    header {{ background: #fff; border-bottom: 1px solid #d9dee7; padding: 16px 24px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    main {{ padding: 18px 24px 36px; }}
    .meta {{ color: #6b7280; font-size: 13px; margin-top: 4px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: end; background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 12px; margin-bottom: 14px; }}
    label {{ display: grid; gap: 4px; color: #4b5563; font-size: 12px; }}
    input, select, textarea {{ border: 1px solid #cfd6e0; border-radius: 6px; padding: 7px 9px; min-height: 34px; background: #fff; }}
    textarea {{ min-width: min(620px, 72vw); resize: vertical; font-family: inherit; }}
    button, .button {{ border: 0; border-radius: 6px; background: #1f5fbf; color: white; padding: 8px 12px; text-decoration: none; font-size: 14px; cursor: pointer; }}
    .button.secondary {{ background: #374151; }}
    .notice {{ background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; border-radius: 8px; padding: 10px 12px; margin-bottom: 14px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .metric {{ background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 12px; }}
    .metric b {{ display: block; font-size: 24px; margin-bottom: 2px; }}
    .metric span {{ color: #6b7280; font-size: 13px; }}
    .panel {{ background: #fff; border: 1px solid #d9dee7; border-radius: 8px; margin-bottom: 14px; overflow: hidden; }}
    .panel h2 {{ font-size: 16px; margin: 0; padding: 12px 14px; border-bottom: 1px solid #e5e7eb; }}
    .panel-body {{ padding: 12px 14px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 9px 8px; text-align: left; vertical-align: top; }}
    th {{ color: #374151; background: #f9fafb; font-weight: 700; white-space: nowrap; }}
    td {{ line-height: 1.5; }}
    .status {{ display: inline-block; white-space: nowrap; font-weight: 700; }}
    .risk {{ display: inline-block; margin: 0 4px 4px 0; padding: 2px 7px; border-radius: 999px; background: #fff7ed; color: #9a3412; border: 1px solid #fed7aa; }}
    .risk.high {{ background: #fef2f2; color: #991b1b; border-color: #fecaca; }}
    .risk.medium {{ background: #fffbeb; color: #92400e; border-color: #fde68a; }}
    .risk.low {{ background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }}
    .empty {{ color: #9ca3af; }}
    .work {{ max-width: 260px; }}
    .check-cell {{ text-align: center; width: 48px; }}
    .test-form {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }}
    .hint {{ color: #6b7280; font-size: 12px; margin-top: 8px; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ margin: 5px 0; }}
    @media (max-width: 960px) {{
      .metrics {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      main {{ padding: 14px; }}
      table {{ min-width: 1100px; }}
      .panel.table-wrap {{ overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>苏州金螳螂法务部 AI 每日复盘管理台</h1>
    <div class="meta">日期：{target_date.isoformat()} · 内部运营视图 · 当前筛选 {len(filtered_rows)} 人</div>
  </header>
  <main>
    {notice_html}
    <form class="toolbar" method="get" action="/admin/reports">
      <label>日期<input type="date" name="report_date" value="{target_date.isoformat()}" /></label>
      <label>团队{_team_select(teams, team_id)}</label>
      <label>状态{_status_select(status)}</label>
      <label>风险{_risk_select(risk)}</label>
      <button type="submit">查询</button>
      <a class="button secondary" href="{escape(export_url)}">导出数据表</a>
      <a class="button secondary" href="{escape(briefing_url)}">团队/部门汇报预览</a>
      <a class="button secondary" href="{escape(learning_url)}">系统学习观察</a>
    </form>
    <section class="panel">
      <h2>钉钉测试发送</h2>
      <div class="panel-body">
        <form id="test-send-form" class="test-form" method="post" action="/admin/test-message">
          <input type="hidden" name="report_date" value="{target_date.isoformat()}" />
          <input type="hidden" name="team_id" value="{escape(team_id)}" />
          <input type="hidden" name="status" value="{escape(status)}" />
          <input type="hidden" name="risk" value="{escape(risk)}" />
          <label>测试文案<textarea name="message" rows="2">{escape(DEFAULT_TEST_MESSAGE)}</textarea></label>
          <button type="submit" onclick="return confirm('确定给勾选人员发送钉钉测试消息？')">发送钉钉测试</button>
        </form>
        <div class="hint">只发送给下方表格中手动勾选的人员；未勾选不会发送，scheduler 不会被启用。</div>
      </div>
    </section>
    <section class="metrics">
      {_metric("总人数", stats["total"])}
      {_metric("已完成", stats["completed"])}
      {_metric("待确认", stats["pending_confirmation"])}
      {_metric("填写中", stats["collecting"])}
      {_metric("未开始", stats["missing"])}
      {_metric("高风险", stats["high_risk"])}
    </section>
    <section class="panel">
      <h2>风险提示</h2>
      <div class="panel-body">
        <ul>{''.join(risk_items) if risk_items else '<li class="empty">当前筛选范围内暂无风险标签</li>'}</ul>
      </div>
    </section>
    <section class="panel table-wrap">
      <h2>个人日报完成情况</h2>
      <table>
        <thead>
          <tr><th>测试</th><th>团队</th><th>姓名</th><th>角色</th><th>状态</th><th>风险标签</th><th>今日工作</th><th>问题/风险</th><th>明日计划</th><th>确认方式</th><th>质量提醒</th></tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


@router.get("/learning", response_class=HTMLResponse)
async def learning_dashboard(
    report_date: date | None = None,
    user_id: str = "all",
    notice: str = "",
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> str:
    target_date = report_date or today_in_timezone(settings.timezone)
    selected_user_id = _parse_uuid(user_id)
    users = await get_active_users(session)
    events = await _load_learning_events(session, target_date, selected_user_id)
    habits = await _load_learning_habits(session, selected_user_id)
    stats = _learning_stats(events, habits)
    attention_items = _learning_attention_items(events, habits)
    attention_html = "".join(f"<li>{item}</li>" for item in attention_items) or '<li class="empty">暂无需要重点关注的学习信号</li>'
    events_html = "".join(_learning_event_row(event) for event in events) or '<tr><td colspan="9" class="empty">暂无观察记录</td></tr>'
    habits_html = "".join(_habit_row(habit, target_date=target_date, selected_user_id=user_id) for habit in habits) or '<tr><td colspan="8" class="empty">暂无候选习惯</td></tr>'
    notice_html = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>系统学习观察</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; color: #1f2937; background: #f4f6f8; }}
    header {{ background: #fff; border-bottom: 1px solid #d9dee7; padding: 16px 24px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    main {{ padding: 18px 24px 36px; }}
    .meta, .hint {{ color: #6b7280; font-size: 13px; margin-top: 4px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: end; background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 12px; margin-bottom: 14px; }}
    label {{ display: grid; gap: 4px; color: #4b5563; font-size: 12px; }}
    input, select {{ border: 1px solid #cfd6e0; border-radius: 6px; padding: 7px 9px; min-height: 34px; background: #fff; }}
    button, .button {{ border: 0; border-radius: 6px; background: #1f5fbf; color: white; padding: 8px 12px; text-decoration: none; font-size: 14px; cursor: pointer; }}
    .button.secondary {{ background: #374151; }}
    .button.inline, button.inline {{ padding: 5px 8px; font-size: 12px; margin: 0 4px 4px 0; }}
    button.danger {{ background: #b91c1c; }}
    button.muted {{ background: #6b7280; }}
    form.inline {{ display: inline; }}
    .notice {{ background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; border-radius: 8px; padding: 10px 12px; margin-bottom: 14px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .metric {{ background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 12px; }}
    .metric b {{ display: block; font-size: 24px; margin-bottom: 2px; }}
    .metric span {{ color: #6b7280; font-size: 13px; }}
    .panel {{ background: #fff; border: 1px solid #d9dee7; border-radius: 8px; margin-bottom: 14px; overflow: hidden; }}
    .panel h2 {{ font-size: 16px; margin: 0; padding: 12px 14px; border-bottom: 1px solid #e5e7eb; }}
    .panel-body {{ padding: 12px 14px; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ margin: 6px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 9px 8px; text-align: left; vertical-align: top; }}
    th {{ color: #374151; background: #f9fafb; font-weight: 700; white-space: nowrap; }}
    td {{ line-height: 1.5; }}
    .empty {{ color: #9ca3af; }}
    .pill {{ display: inline-block; margin: 0 4px 4px 0; padding: 2px 7px; border-radius: 999px; border: 1px solid #cfd6e0; background: #f9fafb; }}
    .warn {{ background: #fff7ed; border-color: #fed7aa; color: #9a3412; }}
    .bad {{ background: #fef2f2; border-color: #fecaca; color: #991b1b; }}
    .good {{ background: #ecfdf5; border-color: #a7f3d0; color: #065f46; }}
    .text {{ max-width: 300px; white-space: pre-wrap; }}
    @media (max-width: 960px) {{
      .metrics {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      main {{ padding: 14px; }}
      table {{ min-width: 1050px; }}
      .panel {{ overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>AI 日报系统学习观察</h1>
    <div class="meta">日期：{target_date.isoformat()} · 只读观察视图 · 不会自动改规则</div>
  </header>
  <main>
    {notice_html}
    <form class="toolbar" method="get" action="/admin/learning">
      <label>日期<input type="date" name="report_date" value="{target_date.isoformat()}" /></label>
      <label>成员{_user_select(users, user_id)}</label>
      <button type="submit">查看</button>
      <a class="button secondary" href="/admin/reports?report_date={target_date.isoformat()}">返回日报管理台</a>
    </form>
    <section class="metrics">
      {_metric("交互数", stats["events"])}
      {_metric("低置信", stats["low_confidence"])}
      {_metric("纠错", stats["corrections"])}
      {_metric("撤回", stats["undos"])}
      {_metric("候选习惯", stats["habit_candidates"])}
      {_metric("已启用习惯", stats["active_habits"])}
    </section>
    <section class="panel">
      <h2>今日重点关注</h2>
      <div class="panel-body"><ul>{attention_html}</ul></div>
    </section>
    <section class="panel">
      <h2>最近交互观察</h2>
      <table>
        <thead><tr><th>时间</th><th>用户</th><th>输入</th><th>后端动作</th><th>置信度</th><th>标记</th><th>纠错</th><th>变更前</th><th>变更后</th></tr></thead>
        <tbody>{events_html}</tbody>
      </table>
    </section>
    <section class="panel">
      <h2>用户习惯沉淀</h2>
      <div class="hint" style="padding: 10px 14px;">candidate 是系统观察到的候选习惯；active 会进入该用户后续对话上下文；rejected/disabled 只保留证据，不参与理解。</div>
      <table>
        <thead><tr><th>用户</th><th>类型</th><th>触发表达</th><th>含义</th><th>置信度</th><th>证据</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>{habits_html}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


@router.get("/learning.json")
async def learning_dashboard_json(
    report_date: date | None = None,
    user_id: str = "all",
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    target_date = report_date or today_in_timezone(settings.timezone)
    selected_user_id = _parse_uuid(user_id)
    events = await _load_learning_events(session, target_date, selected_user_id)
    habits = await _load_learning_habits(session, selected_user_id)
    return {
        "report_date": target_date.isoformat(),
        "stats": _learning_stats(events, habits),
        "attention_items": [_strip_html(item) for item in _learning_attention_items(events, habits)],
        "events": [_learning_event_payload(row) for row in events[:50]],
        "habits": [_habit_payload(row) for row in habits[:50]],
    }


@router.post("/learning/habits/{habit_id}/status")
async def update_learning_habit_status(
    habit_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    target_status = _form_value(form, "status", "candidate")
    report_date = _form_value(form, "report_date")
    user_id = _form_value(form, "user_id", "all")
    habit_uuid = _parse_uuid(habit_id)
    if habit_uuid is None:
        return _redirect_to_learning(report_date, user_id, "习惯 ID 无效，未更新。")
    try:
        habit = await set_user_habit_status(session, habit_uuid, target_status)
        await session.commit()
    except ValueError:
        await session.rollback()
        return _redirect_to_learning(report_date, user_id, "习惯状态无效，未更新。")
    if habit is None:
        return _redirect_to_learning(report_date, user_id, "没有找到这条用户习惯，未更新。")
    status_label = {
        "candidate": "候选",
        "active": "已启用",
        "rejected": "已拒绝",
        "disabled": "已停用",
    }.get(target_status, target_status)
    return _redirect_to_learning(report_date, user_id, f"已将用户习惯更新为：{status_label}。")


@router.get("/reports.csv")
async def export_reports_csv(
    report_date: date | None = None,
    team_id: str = "all",
    status: str = "all",
    risk: str = "all",
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    target_date = report_date or today_in_timezone(settings.timezone)
    _teams, rows = await _load_dashboard_rows(session, target_date, team_id=team_id)
    rows = _filter_rows(rows, status=status, risk=risk)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["日期", "团队", "姓名", "角色", "状态", "风险标签", "今日工作", "问题/风险", "明日计划", "确认方式", "主动确认", "质量提醒"])
    for row in rows:
        user = row["user"]
        team = row["team"]
        report = row["report"]
        writer.writerow(
            [
                target_date.isoformat(),
                getattr(team, "name", ""),
                user.name,
                user.role,
                row["status_label"],
                row["risk_summary"],
                format_items(row["today_work"]),
                format_items(row["problems"], empty="暂无明显问题" if report and (report.section_status or {}).get("problems_acknowledged_empty") else ""),
                format_items(row["tomorrow_plan"]),
                row["confirmation_type"],
                "是" if row["confirmed_by_user"] else "否",
                row["quality_warning"],
            ]
        )
    filename = f"daily-reports-{target_date.isoformat()}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/test-message")
async def send_admin_test_message(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    report_date = _form_value(form, "report_date")
    team_id = _form_value(form, "team_id", "all")
    status = _form_value(form, "status", "all")
    risk = _form_value(form, "risk", "all")
    message = _form_value(form, "message", DEFAULT_TEST_MESSAGE).strip() or DEFAULT_TEST_MESSAGE
    message = message[:1000]

    selected_ids = {_parse_uuid(str(value)) for value in form.get("user_ids", [])}
    selected_ids.discard(None)
    if not selected_ids:
        return _redirect_to_reports(report_date, team_id, status, risk, "请先在表格中勾选要发送测试消息的人员。")

    users = await get_active_users(session)
    targets = [user for user in users if user.id in selected_ids and getattr(user, "dingtalk_user_id", None)]
    skipped = len(selected_ids) - len(targets)
    if not targets:
        return _redirect_to_reports(report_date, team_id, status, risk, "已勾选人员没有可用的钉钉 user_id，未发送。")
    if len(targets) > 10:
        return _redirect_to_reports(report_date, team_id, status, risk, "为避免误发，一次最多勾选 10 人发送测试消息。")

    client = DingTalkRobotClient(settings)
    try:
        await client.send_robot_direct_text(user_ids=[user.dingtalk_user_id for user in targets], text=message)
    except Exception as exc:  # pragma: no cover - exercised by integration/manual tests
        return _redirect_to_reports(report_date, team_id, status, risk, f"钉钉测试消息发送失败：{exc}")
    finally:
        await client.close()

    names = "、".join(user.name for user in targets)
    suffix = f"；跳过 {skipped} 个无钉钉 user_id 的人员" if skipped else ""
    return _redirect_to_reports(report_date, team_id, status, risk, f"已发送钉钉测试消息给 {len(targets)} 人：{names}{suffix}")


async def _load_dashboard_rows(session: AsyncSession, target_date: date, *, team_id: str) -> tuple[list, list[dict]]:
    teams = await get_active_teams(session)
    users = await get_active_users(session)
    team_uuid = _parse_uuid(team_id)
    if team_uuid:
        users = [user for user in users if user.team_id == team_uuid]
    reports = await list_reports_for_date(session, target_date, team_uuid)
    history_start = target_date - timedelta(days=3)
    history_end = target_date - timedelta(days=1)
    history = await list_reports_between_dates(session, history_start, history_end, team_uuid)
    return teams, build_report_rows(users, reports, historical_reports=history)


def _filter_rows(rows: list[dict], *, status: str, risk: str) -> list[dict]:
    filtered = rows
    if status != "all":
        filtered = [row for row in filtered if row["status"] == status]
    if risk == "with_risk":
        filtered = [row for row in filtered if row["risks"]]
    elif risk == "high":
        filtered = [row for row in filtered if any(item["severity"] == "high" for item in row["risks"])]
    elif risk == "none":
        filtered = [row for row in filtered if not row["risks"]]
    return filtered


def _table_row(row: dict) -> str:
    user = row["user"]
    team = row["team"]
    report = row["report"]
    problems = format_items(row["problems"], empty="暂无明显问题" if report and (report.section_status or {}).get("problems_acknowledged_empty") else "")
    dingtalk_user_id = getattr(user, "dingtalk_user_id", None)
    checkbox = (
        f'<input type="checkbox" name="user_ids" value="{escape(str(user.id))}" form="test-send-form" />'
        if dingtalk_user_id
        else '<span class="empty" title="缺少钉钉 user_id">不可发</span>'
    )
    cells = [
        checkbox,
        escape(getattr(team, "name", "")),
        escape(user.name),
        escape(user.role),
        f'<span class="status">{escape(row["status_label"])}</span>',
        _risk_chips(row["risks"]),
        escape(format_items(row["today_work"])),
        escape(problems),
        escape(format_items(row["tomorrow_plan"])),
        escape(row["confirmation_type"]),
        escape(row["quality_warning"] or ""),
    ]
    return "<tr>" + "".join(f'<td class="{"check-cell" if index == 0 else "work"}">{cell}</td>' for index, cell in enumerate(cells)) + "</tr>"


def _risk_chips(risks: list[dict[str, str]]) -> str:
    if not risks:
        return '<span class="empty">无</span>'
    return "".join(
        f'<span class="risk {escape(risk["severity"])}" title="{escape(risk["reason"])}">{escape(risk["label"])}</span>'
        for risk in risks
    )


def _risk_summary_items(rows: list[dict]) -> list[str]:
    items: list[str] = []
    for row in rows:
        if not row["risks"]:
            continue
        risk_text = "、".join(f'{risk["label"]}（{risk["reason"]}）' for risk in row["risks"][:3])
        team_name = getattr(row["team"], "name", "")
        items.append(f"<li><strong>{escape(team_name)} / {escape(row['user'].name)}</strong>：{escape(risk_text)}</li>")
    return items[:20]


async def _load_learning_events(session: AsyncSession, target_date: date, user_id: uuid.UUID | None) -> list:
    stmt = (
        select(ReportInteractionEvent, User)
        .outerjoin(User, User.id == ReportInteractionEvent.user_id)
        .where(ReportInteractionEvent.report_date == target_date)
        .order_by(desc(ReportInteractionEvent.created_at))
        .limit(100)
    )
    if user_id is not None:
        stmt = stmt.where(ReportInteractionEvent.user_id == user_id)
    result = await session.execute(stmt)
    return list(result.all())


async def _load_learning_habits(session: AsyncSession, user_id: uuid.UUID | None) -> list:
    stmt = (
        select(UserHabit, User)
        .join(User, User.id == UserHabit.user_id)
        .order_by(desc(UserHabit.status == "active"), desc(UserHabit.confidence), desc(UserHabit.updated_at))
        .limit(100)
    )
    if user_id is not None:
        stmt = stmt.where(UserHabit.user_id == user_id)
    result = await session.execute(stmt)
    return list(result.all())


def _learning_stats(events: list, habits: list) -> dict[str, int]:
    low_confidence = 0
    corrections = 0
    undos = 0
    active_habits = 0
    candidate_habits = 0
    for event, _user in events:
        confidence = getattr(event, "confidence", None)
        if confidence is not None and float(confidence) < 0.55:
            low_confidence += 1
        if getattr(event, "correction_type", ""):
            corrections += 1
        if getattr(event, "is_undo", False):
            undos += 1
    for habit, _user in habits:
        if getattr(habit, "status", "") == "active":
            active_habits += 1
        elif getattr(habit, "status", "") == "candidate":
            candidate_habits += 1
    return {
        "events": len(events),
        "low_confidence": low_confidence,
        "corrections": corrections,
        "undos": undos,
        "habit_candidates": candidate_habits,
        "active_habits": active_habits,
        "total_habits": len(habits),
    }


def _learning_attention_items(events: list, habits: list) -> list[str]:
    by_user: dict[str, dict[str, int]] = {}
    examples: dict[str, list[str]] = {}
    for event, user in events:
        name = getattr(user, "name", "") if user else str(getattr(event, "user_id", ""))[:8]
        bucket = by_user.setdefault(name, {"low": 0, "correction": 0, "undo": 0, "repeat": 0})
        confidence = getattr(event, "confidence", None)
        if confidence is not None and float(confidence) < 0.55:
            bucket["low"] += 1
            examples.setdefault(name, []).append(_truncate(getattr(event, "message_text", ""), 36))
        if getattr(event, "correction_type", ""):
            bucket["correction"] += 1
            examples.setdefault(name, []).append(_truncate(getattr(event, "message_text", ""), 36))
        if getattr(event, "is_undo", False):
            bucket["undo"] += 1
        if getattr(event, "is_repeated_item_edit", False):
            bucket["repeat"] += 1

    items: list[str] = []
    for name, counts in sorted(by_user.items(), key=lambda item: (sum(item[1].values()), item[0]), reverse=True):
        signals: list[str] = []
        if counts["low"]:
            signals.append(f"低置信 {counts['low']} 次")
        if counts["correction"]:
            signals.append(f"纠错 {counts['correction']} 次")
        if counts["undo"]:
            signals.append(f"撤回 {counts['undo']} 次")
        if counts["repeat"]:
            signals.append(f"重复编辑 {counts['repeat']} 次")
        if not signals:
            continue
        sample = "；例：" + " / ".join(escape(item) for item in examples.get(name, [])[:2] if item) if examples.get(name) else ""
        items.append(f"<strong>{escape(name)}</strong>：{'，'.join(signals)}{sample}")

    near_active: list[str] = []
    active: list[str] = []
    for habit, user in habits:
        name = getattr(user, "name", "") if user else str(getattr(habit, "user_id", ""))[:8]
        status = getattr(habit, "status", "")
        evidence_count = int(getattr(habit, "evidence_count", 0) or 0)
        confidence = float(getattr(habit, "confidence", 0) or 0)
        text = f"<strong>{escape(name)}</strong>：{escape(getattr(habit, 'trigger_text', ''))} → {escape(_truncate(getattr(habit, 'meaning', ''), 80))}"
        if status == "active":
            active.append(text)
        elif evidence_count >= 2 or confidence >= 0.7:
            near_active.append(text)
    if near_active:
        items.append("接近可激活习惯：" + "；".join(near_active[:5]))
    if active:
        items.append("已进入上下文的用户习惯：" + "；".join(active[:5]))
    return items[:12]


def _learning_event_row(row: tuple) -> str:
    event, user = row
    flags: list[str] = []
    confidence = getattr(event, "confidence", None)
    if confidence is not None and float(confidence) < 0.55:
        flags.append('<span class="pill warn">低置信</span>')
    if getattr(event, "is_undo", False):
        flags.append('<span class="pill warn">撤回</span>')
    if getattr(event, "is_repeated_item_edit", False):
        flags.append('<span class="pill warn">重复编辑</span>')
    if getattr(event, "correction_type", ""):
        flags.append('<span class="pill bad">纠错</span>')
    correction = ""
    if getattr(event, "correction_type", ""):
        correction = f'{event.correction_type}: {event.correction_from} -> {event.correction_to}'
    before = _snapshot_summary(getattr(event, "before_snapshot_json", {}) or {})
    after = _snapshot_summary(getattr(event, "after_snapshot_json", {}) or {})
    cells = [
        _format_datetime(getattr(event, "created_at", None)),
        escape(getattr(user, "name", "") if user else str(getattr(event, "user_id", ""))[:8]),
        escape(_truncate(getattr(event, "message_text", ""), 160)),
        escape(getattr(event, "backend_action", "") or ""),
        "" if confidence is None else escape(f"{float(confidence):.2f}"),
        "".join(flags) or '<span class="empty">无</span>',
        escape(_truncate(correction, 120)),
        escape(before),
        escape(after),
    ]
    return "<tr>" + "".join(f'<td class="text">{cell}</td>' for cell in cells) + "</tr>"


def _habit_row(row: tuple, *, target_date: date, selected_user_id: str) -> str:
    habit, user = row
    status_class = "good" if getattr(habit, "status", "") == "active" else "warn"
    cells = [
        escape(getattr(user, "name", "") if user else str(getattr(habit, "user_id", ""))[:8]),
        escape(getattr(habit, "habit_type", "")),
        escape(getattr(habit, "trigger_text", "")),
        escape(_truncate(getattr(habit, "meaning", ""), 180)),
        escape(f"{float(getattr(habit, 'confidence', 0) or 0):.2f}"),
        escape(f"{getattr(habit, 'evidence_count', 0)}/{getattr(habit, 'counterexample_count', 0)}"),
        f'<span class="pill {status_class}">{escape(getattr(habit, "status", ""))}</span>',
        _habit_action_buttons(habit, target_date=target_date, selected_user_id=selected_user_id),
    ]
    return "<tr>" + "".join(f'<td class="text">{cell}</td>' for cell in cells) + "</tr>"


def _habit_action_buttons(habit, *, target_date: date, selected_user_id: str) -> str:
    habit_id = escape(str(getattr(habit, "id", "") or ""))
    if not habit_id:
        return '<span class="empty">无</span>'
    status = str(getattr(habit, "status", "") or "")
    buttons: list[str] = []
    if status == "candidate":
        buttons.append(_habit_status_button(habit_id, "active", "启用", target_date=target_date, selected_user_id=selected_user_id))
        buttons.append(_habit_status_button(habit_id, "rejected", "拒绝", target_date=target_date, selected_user_id=selected_user_id, css_class="danger"))
    elif status == "active":
        buttons.append(_habit_status_button(habit_id, "disabled", "停用", target_date=target_date, selected_user_id=selected_user_id, css_class="muted"))
    elif status in {"rejected", "disabled"}:
        buttons.append(_habit_status_button(habit_id, "candidate", "恢复候选", target_date=target_date, selected_user_id=selected_user_id, css_class="muted"))
    return "".join(buttons) or '<span class="empty">无</span>'


def _habit_status_button(
    habit_id: str,
    status: str,
    label: str,
    *,
    target_date: date,
    selected_user_id: str,
    css_class: str = "",
) -> str:
    classes = "inline" + (f" {css_class}" if css_class else "")
    return f"""
      <form class="inline" method="post" action="/admin/learning/habits/{habit_id}/status">
        <input type="hidden" name="status" value="{escape(status)}" />
        <input type="hidden" name="report_date" value="{target_date.isoformat()}" />
        <input type="hidden" name="user_id" value="{escape(selected_user_id or 'all')}" />
        <button class="{classes}" type="submit">{escape(label)}</button>
      </form>
    """


def _learning_event_payload(row: tuple) -> dict:
    event, user = row
    confidence = getattr(event, "confidence", None)
    return {
        "created_at": str(getattr(event, "created_at", "") or ""),
        "user_name": getattr(user, "name", "") if user else "",
        "message_text": getattr(event, "message_text", "") or "",
        "backend_action": getattr(event, "backend_action", "") or "",
        "confidence": None if confidence is None else float(confidence),
        "correction_type": getattr(event, "correction_type", "") or "",
        "correction_from": getattr(event, "correction_from", "") or "",
        "correction_to": getattr(event, "correction_to", "") or "",
        "is_undo": bool(getattr(event, "is_undo", False)),
        "is_repeated_item_edit": bool(getattr(event, "is_repeated_item_edit", False)),
        "before": _snapshot_summary(getattr(event, "before_snapshot_json", {}) or {}),
        "after": _snapshot_summary(getattr(event, "after_snapshot_json", {}) or {}),
    }


def _habit_payload(row: tuple) -> dict:
    habit, user = row
    return {
        "user_name": getattr(user, "name", "") if user else "",
        "habit_type": getattr(habit, "habit_type", "") or "",
        "trigger_text": getattr(habit, "trigger_text", "") or "",
        "meaning": getattr(habit, "meaning", "") or "",
        "confidence": float(getattr(habit, "confidence", 0) or 0),
        "evidence_count": int(getattr(habit, "evidence_count", 0) or 0),
        "counterexample_count": int(getattr(habit, "counterexample_count", 0) or 0),
        "status": getattr(habit, "status", "") or "",
    }


def _snapshot_summary(snapshot: dict) -> str:
    if not snapshot:
        return ""
    today_count = len(snapshot.get("today_work") or [])
    problems = [str(item or "").strip() for item in (snapshot.get("problems") or []) if str(item or "").strip()]
    problem_count = 0 if _is_no_problem_placeholder_list(problems) else len(problems)
    problem_suffix = "(无风险)" if problems and problem_count == 0 else ""
    plan_count = len(snapshot.get("tomorrow_plan") or [])
    status = snapshot.get("status") or ""
    return f"{status}｜今日{today_count} 问题{problem_count}{problem_suffix} 明日{plan_count}"


def _is_no_problem_placeholder_list(values: list[str]) -> bool:
    placeholders = {"暂无明显问题", "无明显问题", "暂无问题", "无问题", "没问题", "没有问题", "没啥问题", "无明显风险"}
    return bool(values) and all(value in placeholders for value in values)


def _format_datetime(value) -> str:
    if not value:
        return ""
    try:
        return value.strftime("%H:%M:%S")
    except AttributeError:
        return str(value)


def _truncate(value: str, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", str(value or ""))


def _metric(label: str, value: int) -> str:
    return f'<div class="metric"><b>{value}</b><span>{escape(label)}</span></div>'


def _team_select(teams: list, selected: str) -> str:
    options = ['<option value="all">全部团队</option>']
    for team in teams:
        value = str(team.id)
        attr = " selected" if selected == value else ""
        options.append(f'<option value="{escape(value)}"{attr}>{escape(team.name)}</option>')
    return f'<select name="team_id">{"".join(options)}</select>'


def _user_select(users: list, selected: str) -> str:
    options = ['<option value="all">全部成员</option>']
    for user in users:
        value = str(user.id)
        attr = " selected" if selected == value else ""
        options.append(f'<option value="{escape(value)}"{attr}>{escape(user.name)}</option>')
    return f'<select name="user_id">{"".join(options)}</select>'


def _status_select(selected: str) -> str:
    options = [
        ("all", "全部状态"),
        ("completed", "已完成"),
        ("pending_confirmation", "待确认"),
        ("collecting", "填写中"),
        ("missing", "未开始"),
    ]
    return _select("status", options, selected)


def _risk_select(selected: str) -> str:
    options = [
        ("all", "全部"),
        ("with_risk", "有风险"),
        ("high", "高风险"),
        ("none", "无风险"),
    ]
    return _select("risk", options, selected)


def _select(name: str, options: list[tuple[str, str]], selected: str) -> str:
    html = []
    for value, label in options:
        attr = " selected" if selected == value else ""
        html.append(f'<option value="{escape(value)}"{attr}>{escape(label)}</option>')
    return f'<select name="{escape(name)}">{"".join(html)}</select>'


def _redirect_to_reports(report_date: str, team_id: str, status: str, risk: str, notice: str) -> RedirectResponse:
    query = {
        "team_id": team_id or "all",
        "status": status or "all",
        "risk": risk or "all",
        "notice": notice,
    }
    if report_date:
        query["report_date"] = report_date
    return RedirectResponse("/admin/reports?" + urlencode(query), status_code=303)


def _redirect_to_learning(report_date: str, user_id: str, notice: str) -> RedirectResponse:
    query = {
        "user_id": user_id or "all",
        "notice": notice,
    }
    if report_date:
        query["report_date"] = report_date
    return RedirectResponse("/admin/learning?" + urlencode(query), status_code=303)


def _form_value(form: dict[str, list[str]], key: str, default: str = "") -> str:
    values = form.get(key)
    if not values:
        return default
    return str(values[0] or default)


def _parse_uuid(value: str) -> uuid.UUID | None:
    if not value or value == "all":
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
