from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import DailyReport
from app.repositories import get_active_user_by_dingtalk_id
from app.services import report_service
from app.utils.time import clear_time_override, get_time_override, now_in_timezone, set_time_override

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/manual", response_class=HTMLResponse, include_in_schema=False)
async def manual_test_console() -> HTMLResponse:
    settings = get_settings()
    if not settings.clock_override_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    html = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>日报系统 Staging 测试面板</title>
  <style>
    body { margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #15181d; }
    main { max-width: 960px; margin: 0 auto; padding: 28px 18px 48px; }
    h1 { font-size: 22px; margin: 0 0 18px; }
    .version { display: inline-flex; margin-left: 8px; padding: 3px 7px; border-radius: 999px; background: #eef4ff; color: #1849a9; font-size: 12px; vertical-align: middle; }
    section { background: #fff; border: 1px solid #dde1e7; border-radius: 8px; padding: 16px; margin-bottom: 14px; }
    label { display: block; font-size: 13px; font-weight: 650; margin-bottom: 6px; color: #303640; }
    input, textarea { width: 100%; box-sizing: border-box; border: 1px solid #c9d0da; border-radius: 6px; padding: 10px 11px; font: inherit; background: #fff; }
    textarea { min-height: 96px; resize: vertical; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    button { border: 0; border-radius: 6px; padding: 9px 13px; font-weight: 700; cursor: pointer; background: #1769e0; color: white; }
    button.secondary { background: #384252; }
    button.danger { background: #b42318; }
    pre { overflow: auto; white-space: pre-wrap; word-break: break-word; background: #101828; color: #e6edf7; padding: 14px; border-radius: 8px; max-height: 320px; }
    details { margin-top: 12px; }
    summary { cursor: pointer; color: #344054; font-weight: 650; }
    .muted { color: #667085; font-size: 13px; }
    .result { display: grid; gap: 12px; }
    .status-line { display: flex; flex-wrap: wrap; gap: 8px; }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 5px 9px; font-size: 12px; font-weight: 700; background: #eef4ff; color: #1849a9; }
    .pill.good { background: #ecfdf3; color: #027a48; }
    .pill.warn { background: #fff6ed; color: #c4320a; }
    .pill.bad { background: #fef3f2; color: #b42318; }
    .reply { border-left: 3px solid #1769e0; background: #f8fbff; padding: 10px 12px; border-radius: 6px; white-space: pre-wrap; }
    .sections { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .field { border: 1px solid #e4e7ec; border-radius: 8px; padding: 11px; background: #fcfcfd; }
    .field h2 { font-size: 14px; margin: 0 0 8px; }
    .field ol { margin: 0; padding-left: 20px; }
    .field li { margin: 4px 0; line-height: 1.45; }
    .empty { color: #98a2b3; }
    .error-box { background: #fef3f2; color: #912018; border: 1px solid #fecdca; border-radius: 8px; padding: 12px; }
    @media (max-width: 700px) { .row { grid-template-columns: 1fr; } }
    @media (max-width: 900px) { .sections { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <h1>日报系统 Staging 测试面板<span class="version">v2026-06-18-1135</span></h1>
    <section>
      <div class="row">
        <div>
          <label for="userId">测试账号</label>
          <input id="userId" value="staging-test-user-001" />
        </div>
        <div>
          <label for="now">模拟时间（发送前会自动应用）</label>
          <input id="now" value="2026-06-18T08:31:00+08:00" />
        </div>
      </div>
      <div class="actions">
        <button id="setTime">设置时间</button>
        <button class="secondary" id="getTime">查看时间</button>
        <button class="danger" id="resetData">清空该账号日报</button>
      </div>
    </section>
    <section>
      <label for="message">发送消息</label>
      <textarea id="message">昨天完成线上流程审批92个协议用印是三个，协议的归档是四个。然后还对法务审批关键节点的培训，PPT的制作困难点进行了分析</textarea>
      <div class="actions">
        <button id="sendMessage">发送</button>
      </div>
      <p class="muted">连续发送多句话时，只改上面的消息内容再点发送即可。每次发送前都会先应用上方的模拟时间。</p>
    </section>
    <section>
      <label>返回结果</label>
      <div id="summary" class="muted">等待操作...</div>
      <details>
        <summary>查看原始 JSON</summary>
        <pre id="output">等待操作...</pre>
      </details>
    </section>
  </main>
  <script>
    const summary = document.getElementById("summary");
    const output = document.getElementById("output");
    const replyKindLabel = {
      followup: "已收集，等待继续补充",
      problem_clarification: "需要补充问题/风险",
      previous_report_cutoff: "已拦截，超过 9 点不能改昨天",
      report_date_clarification: "日期已确认，等待内容",
      non_report_interaction: "无需调整日报",
      draft_decision_rejected: "未修改，定位失败",
      draft_decision_failed: "未写入，理解失败",
      pending_interaction: "需要继续说明",
      history_query: "历史查询"
    };
    const escapeHtml = (value) => String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
    const renderList = (items) => {
      if (!Array.isArray(items) || items.length === 0) return '<div class="empty">暂无</div>';
      return `<ol>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol>`;
    };
    const kindClass = (kind) => {
      if (kind === "previous_report_cutoff" || kind === "draft_decision_rejected" || kind === "draft_decision_failed") return "bad";
      if (kind === "problem_clarification" || kind === "pending_interaction") return "warn";
      return "good";
    };
    const renderReportResult = (payload) => {
      const report = payload.merged_report || {};
      const timings = payload.timings || {};
      const kind = payload.reply_kind || "";
      summary.className = "result";
      summary.innerHTML = `
        <div class="status-line">
          <span class="pill">日报日期：${escapeHtml(payload.report_date || "-")}</span>
          <span class="pill ${kindClass(kind)}">${escapeHtml(replyKindLabel[kind] || kind || "返回成功")}</span>
          <span class="pill">状态：${escapeHtml(payload.status || "-")}</span>
          <span class="pill">模型：${escapeHtml(timings.llm_draft_decision_model || timings.llm_extract_model || "未调用")}</span>
        </div>
        <div class="reply">${escapeHtml(payload.message || "无机器人回复")}</div>
        <div class="sections">
          <div class="field"><h2>今日工作</h2>${renderList(report.today_work)}</div>
          <div class="field"><h2>问题/风险</h2>${renderList(report.problems)}</div>
          <div class="field"><h2>明日计划</h2>${renderList(report.tomorrow_plan)}</div>
        </div>
      `;
    };
    const renderGenericResult = (payload) => {
      summary.className = "result";
      if (payload && typeof payload === "object" && "now" in payload) {
        summary.innerHTML = `
          <div class="status-line"><span class="pill good">时间设置成功</span></div>
          <div class="reply">当前时间：${escapeHtml(payload.now)}\n业务服务时间：${escapeHtml(payload.report_service_now)}</div>
        `;
        return;
      }
      if (payload && typeof payload === "object" && "deleted_reports" in payload) {
        summary.innerHTML = `
          <div class="status-line"><span class="pill good">测试数据已清空</span></div>
          <div class="reply">账号：${escapeHtml(payload.dingtalk_user_id)}\n删除日报数：${escapeHtml(payload.deleted_reports)}</div>
        `;
        return;
      }
      summary.innerHTML = `<div class="reply">${escapeHtml(typeof payload === "string" ? payload : JSON.stringify(payload, null, 2))}</div>`;
    };
    const show = (value) => {
      output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      if (value && typeof value === "object" && ("report_date" in value || "merged_report" in value)) {
        renderReportResult(value);
      } else {
        renderGenericResult(value);
      }
    };
    const showLoading = (text) => {
      summary.className = "muted";
      summary.textContent = text;
      output.textContent = text;
    };
    const showError = (error) => {
      output.textContent = typeof error === "string" ? error : JSON.stringify(error, null, 2);
      summary.className = "error-box";
      summary.textContent = typeof error === "string" ? error : (error.detail || JSON.stringify(error, null, 2));
    };
    const postJson = async (url, body) => {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify(body),
      });
      const text = await response.text();
      let payload;
      try { payload = JSON.parse(text); } catch { payload = text; }
      if (!response.ok) throw payload;
      return payload;
    };
    document.getElementById("setTime").onclick = async () => {
      showLoading("正在设置时间...");
      try { show(await postJson("/debug/time/override", { now: document.getElementById("now").value.trim() })); }
      catch (error) { showError(error); }
    };
    document.getElementById("getTime").onclick = async () => {
      showLoading("正在读取时间...");
      try { show(await (await fetch("/debug/time")).json()); }
      catch (error) { showError(String(error)); }
    };
    document.getElementById("resetData").onclick = async () => {
      showLoading("正在清空测试数据...");
      try { show(await postJson("/debug/test-data/reset", { dingtalk_user_id: document.getElementById("userId").value.trim() })); }
      catch (error) { showError(error); }
    };
    document.getElementById("sendMessage").onclick = async () => {
      showLoading("正在设置模拟时间并调用真实 LLM，请稍等...");
      try {
        await postJson("/debug/time/override", { now: document.getElementById("now").value.trim() });
        show(await postJson("/reports/manual", {
          dingtalk_user_id: document.getElementById("userId").value.trim(),
          raw_input: document.getElementById("message").value,
          source: "staging_manual_page"
        }));
      } catch (error) { showError(error); }
    };
  </script>
</body>
</html>
"""
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


class TimeOverrideRequest(BaseModel):
    now: str | None = None


class TestDataResetRequest(BaseModel):
    dingtalk_user_id: str


@router.get("/time")
async def get_debug_time() -> dict[str, str | bool | None]:
    settings = get_settings()
    current = now_in_timezone(settings.timezone)
    override = get_time_override(settings.timezone)
    return {
        "clock_override_enabled": settings.clock_override_enabled,
        "timezone": settings.timezone,
        "now": current.isoformat(),
        "report_service_now": report_service.now_in_timezone(settings.timezone).isoformat(),
        "override": override.isoformat() if override else None,
    }


@router.post("/time/override")
async def set_debug_time_override(body: TimeOverrideRequest) -> dict[str, str | bool | None]:
    settings = get_settings()
    if not settings.clock_override_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Clock override is disabled in this environment.",
        )

    if not body.now:
        clear_time_override()
        current = now_in_timezone(settings.timezone)
        return {
            "clock_override_enabled": True,
            "timezone": settings.timezone,
            "now": current.isoformat(),
            "report_service_now": report_service.now_in_timezone(settings.timezone).isoformat(),
            "override": None,
        }

    try:
        parsed = datetime.fromisoformat(body.now)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use ISO datetime, for example 2026-06-18T08:31:00+08:00.",
        ) from exc
    set_time_override(parsed)
    current = now_in_timezone(settings.timezone)
    return {
        "clock_override_enabled": True,
        "timezone": settings.timezone,
        "now": current.isoformat(),
        "report_service_now": report_service.now_in_timezone(settings.timezone).isoformat(),
        "override": current.isoformat(),
    }


@router.post("/test-data/reset")
async def reset_test_data(
    body: TestDataResetRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, int | str]:
    settings = get_settings()
    if not settings.clock_override_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Test data reset is disabled in this environment.",
        )

    user = await get_active_user_by_dingtalk_id(session, body.dingtalk_user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    result = await session.execute(delete(DailyReport).where(DailyReport.user_id == user.id))
    await session.commit()
    return {
        "dingtalk_user_id": body.dingtalk_user_id,
        "deleted_reports": int(result.rowcount or 0),
    }
