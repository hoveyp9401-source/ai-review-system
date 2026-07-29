from __future__ import annotations

import traceback
import uuid
from datetime import date
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.llm.extractor import LLMOutputError
from app.repositories import (
    create_webhook_event_once,
    get_active_user_by_dingtalk_id,
    mark_webhook_event_failed,
    mark_webhook_event_processed,
)
from app.services.dingtalk import build_idempotency_key, dingtalk_text_response, parse_incoming_message
from app.services.report_service import DailyReportService
from app.services.state_machine import build_followup_message, infer_report_state
from app.utils.time import now_in_timezone

router = APIRouter(prefix="/debug", tags=["debug"])


class DebugSendRequest(BaseModel):
    raw_input: str = Field(min_length=1)
    dingtalk_user_id: str = Field(default="dingtalk-user-001", min_length=1)
    report_date: date | None = None
    message_id: str | None = None


@router.get("", response_class=HTMLResponse)
async def debug_page() -> str:
    return DEBUG_HTML


@router.post("/send")
async def debug_send(
    request: Request,
    body: DebugSendRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    request_id = body.message_id or f"debug-{uuid.uuid4()}"
    request_start = time.perf_counter()
    now = now_in_timezone(settings.timezone)
    simulated_payload = {
        "msgtype": "text",
        "msgId": request_id,
        "senderStaffId": body.dingtalk_user_id,
        "conversationId": "debug-console",
        "text": {"content": body.raw_input},
    }
    chain: dict[str, Any] = {
        "request_id": request_id,
        "webhook_received": True,
        "simulated_payload": simulated_payload,
        "steps": [],
        "db_written": False,
        "error_log": "",
        "report_date": body.report_date.isoformat() if body.report_date else None,
        "timings": {},
    }
    event = None

    try:
        incoming = parse_incoming_message(simulated_payload)
        idempotency_key = build_idempotency_key(simulated_payload, incoming)
        chain["parsed_message"] = {
            "user_id": incoming.dingtalk_user_id,
            "text": incoming.text,
            "message_id": incoming.message_id,
            "source": incoming.source,
        }
        chain["idempotency_key"] = idempotency_key
        chain["steps"].append("webhook payload parsed")

        step_start = time.perf_counter()
        try:
            event, inserted = await create_webhook_event_once(
                session,
                idempotency_key=idempotency_key,
                external_message_id=incoming.message_id,
                dingtalk_user_id=incoming.dingtalk_user_id,
                payload=simulated_payload,
            )
            await session.commit()
            chain["timings"]["idempotency_db_seconds"] = round(time.perf_counter() - step_start, 4)
        except (ConnectionRefusedError, OSError, SQLAlchemyError) as exc:
            await session.rollback()
            chain["db_available"] = False
            chain["db_written"] = False
            chain["error_log"] = (
                "数据库未连接，所以本次只测试 webhook 解析和 LLM 结构化；"
                f"日报没有写入数据库。原始错误：{type(exc).__name__}: {exc}"
            )
            chain["steps"].append("database unavailable; skipped persistence")
            report_service: DailyReportService = request.app.state.report_service
            step_start = time.perf_counter()
            try:
                structured = await report_service.extractor.extract(incoming.text)
                chain["timings"]["llm_extract_seconds"] = round(time.perf_counter() - step_start, 4)
            except Exception as llm_exc:
                chain["steps"].append("LLM request failed")
                chain["response"] = dingtalk_text_response("数据库未连接，且 LLM 调用失败；本次没有写入日报。")
                chain["error_log"] = (
                    chain["error_log"]
                    + f"\nLLM 错误：{type(llm_exc).__name__}: {llm_exc}"
                )
                chain["timings"]["request_total_seconds"] = round(time.perf_counter() - request_start, 4)
                return chain
            state = infer_report_state(
                existing_section_status=None,
                structured=structured,
                raw_input=incoming.text,
                merged_today_work=structured.today_work,
                merged_problems=structured.problems,
                merged_tomorrow_plan=structured.tomorrow_plan,
            )
            if state.status == "pending_confirmation":
                reply_text = "AI 解析已完成，三段内容完整；但数据库未连接，所以没有写入日报。"
            else:
                reply_text = build_followup_message(state.missing_sections)
            chain.update(
                {
                    "llm_json": structured.model_dump(),
                    "today_work": structured.today_work,
                    "problems": structured.problems,
                    "tomorrow_plan": structured.tomorrow_plan,
                    "report_status": state.status,
                    "missing_sections": state.missing_sections,
                    "completeness_score": state.completeness_score,
                    "response": dingtalk_text_response(reply_text),
                }
            )
            chain["steps"].append("LLM structured output validated")
            chain["timings"]["request_total_seconds"] = round(time.perf_counter() - request_start, 4)
            return chain

        chain["db_available"] = True
        chain["deduplicated"] = not inserted
        chain["webhook_event_status"] = event.status
        chain["steps"].append("webhook idempotency checked")

        if not inserted:
            chain["response"] = event.response_payload or dingtalk_text_response("这条调试消息已经处理过或正在处理中。")
            chain["db_written"] = False
            chain["steps"].append("duplicate message returned cached event")
            chain["timings"]["request_total_seconds"] = round(time.perf_counter() - request_start, 4)
            return chain

        step_start = time.perf_counter()
        user = await get_active_user_by_dingtalk_id(session, incoming.dingtalk_user_id)
        chain["timings"]["user_lookup_seconds"] = round(time.perf_counter() - step_start, 4)
        if user is None:
            response_payload = dingtalk_text_response("未识别到调试用户，请先在 users 表维护该 DingTalk user_id。")
            await mark_webhook_event_failed(
                session,
                event,
                error_message=f"Unknown DingTalk user: {incoming.dingtalk_user_id}",
                response_payload=response_payload,
                now=now,
            )
            await session.commit()
            chain["response"] = response_payload
            chain["error_log"] = f"Unknown DingTalk user: {incoming.dingtalk_user_id}"
            chain["steps"].append("user lookup failed")
            chain["timings"]["request_total_seconds"] = round(time.perf_counter() - request_start, 4)
            return chain

        chain["user"] = {
            "id": str(user.id),
            "dingtalk_user_id": user.dingtalk_user_id,
            "name": user.name,
            "team_id": str(user.team_id),
        }
        chain["steps"].append("user loaded")

        report_service: DailyReportService = request.app.state.report_service
        result = await report_service.submit_text(
            session,
            user=user,
            raw_input=incoming.text,
            source="debug_dingtalk_text",
            report_date=body.report_date,
        )
        chain["timings"].update(result.timings)
        chain["steps"].append("LLM structured output validated")

        response_payload = dingtalk_text_response(result.message)
        await mark_webhook_event_processed(
            session,
            event,
            report_id=uuid.UUID(result.report_id) if result.report_id else None,
            response_payload=response_payload,
            now=now,
        )
        await session.commit()
        chain["timings"]["request_total_seconds"] = round(time.perf_counter() - request_start, 4)

        chain.update(
            {
                "db_written": True,
                "report_id": result.report_id,
                "report_date": result.report_date.isoformat(),
                "report_status": result.status,
                "completeness_score": result.completeness_score,
                "missing_sections": result.missing_sections,
                "llm_json": result.structured.model_dump(),
                "today_work": result.today_work,
                "problems": result.problems,
                "tomorrow_plan": result.tomorrow_plan,
                "section_status": result.section_status,
                "problems_display": (
                    result.problems
                    if result.problems
                    else (["无问题/无风险"] if result.section_status.get("problems_acknowledged_empty") else [])
                ),
                "merged_report": {
                    "today_work": result.today_work,
                    "problems": result.problems,
                    "tomorrow_plan": result.tomorrow_plan,
                    "section_status": result.section_status,
                },
                "response": response_payload,
            }
        )
        chain["steps"].append("daily report upserted")
        chain["steps"].append("webhook event marked processed")
        return chain
    except LLMOutputError as exc:
        await session.rollback()
        response_payload = dingtalk_text_response("LLM 输出不符合 JSON schema，调试数据没有写入日报。")
        if event is not None:
            async with session.begin():
                event = await session.merge(event)
                await mark_webhook_event_failed(
                    session,
                    event,
                    error_message=str(exc),
                    response_payload=response_payload,
                    now=now,
                )
        chain["error_log"] = str(exc)
        chain["response"] = response_payload
        chain["timings"]["request_total_seconds"] = round(time.perf_counter() - request_start, 4)
        chain["steps"].append("LLM output validation failed")
        return chain
    except Exception as exc:
        await session.rollback()
        response_payload = dingtalk_text_response("调试处理失败，数据没有写入日报。")
        if event is not None:
            async with session.begin():
                event = await session.merge(event)
                await mark_webhook_event_failed(
                    session,
                    event,
                    error_message=str(exc),
                    response_payload=response_payload,
                    now=now,
                )
        chain["error_log"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        chain["response"] = response_payload
        chain["timings"]["request_total_seconds"] = round(time.perf_counter() - request_start, 4)
        chain["steps"].append("unexpected error")
        return chain


DEBUG_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI复盘系统调试台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dee8;
      --text: #1f2937;
      --muted: #667085;
      --accent: #1f7aec;
      --ok: #0f8a4c;
      --bad: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0; font-size: 20px; }
    main {
      width: min(1180px, calc(100vw - 32px));
      margin: 18px auto 32px;
      display: grid;
      gap: 16px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2 { margin: 0 0 12px; font-size: 16px; }
    label { display: block; margin-bottom: 8px; color: var(--muted); font-size: 13px; }
    textarea, input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      font: inherit;
      background: #fff;
    }
    textarea { min-height: 130px; resize: vertical; }
    .row {
      display: grid;
      grid-template-columns: 1fr 220px;
      gap: 12px;
      align-items: end;
    }
    button {
      height: 42px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: white;
      font: inherit;
      cursor: pointer;
    }
    button:disabled { opacity: .6; cursor: wait; }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .box {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 90px;
      background: #fbfcfe;
    }
    .box h3 { margin: 0 0 8px; font-size: 14px; }
    .reply {
      border: 1px solid #b9d6ff;
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      padding: 12px;
      background: #f3f8ff;
      margin-bottom: 12px;
    }
    .reply h3 { margin: 0 0 6px; font-size: 14px; }
    .reply p { margin: 0; line-height: 1.6; }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }
    .chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 10px;
      background: #fff;
      font-size: 13px;
    }
    .chip.missing {
      border-color: #f2b8b5;
      color: var(--bad);
      background: #fff7f6;
    }
    ul { margin: 0; padding-left: 18px; }
    li { margin: 4px 0; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #111827;
      color: #eef2ff;
      border-radius: 8px;
      padding: 12px;
      max-height: 360px;
      overflow: auto;
    }
    .debug {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .kv {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfe;
    }
    .kv span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }
    .ok { color: var(--ok); font-weight: 700; }
    .bad { color: var(--bad); font-weight: 700; }
    @media (max-width: 760px) {
      .row, .grid, .debug { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>AI复盘系统调试台</h1>
  </header>
  <main>
    <section>
      <h2>1. 输入区</h2>
      <div class="row">
        <div>
          <label for="userId">模拟钉钉 user_id</label>
          <input id="userId" value="dingtalk-user-001">
        </div>
        <button id="sendBtn">发送</button>
      </div>
      <div style="height:12px"></div>
      <label for="reportDate">复盘日期</label>
      <input id="reportDate" type="date">
      <div style="height:12px"></div>
      <label for="rawInput">模拟钉钉消息输入</label>
      <textarea id="rawInput">今天完成了合同审核流程梳理，发现跨部门反馈慢可能影响上线，明天准备整理风险清单并同步业务负责人。</textarea>
    </section>

    <section>
      <h2>2. 输出区</h2>
      <div class="reply">
        <h3>系统追问 / 确认回复</h3>
        <p id="replyText">点击发送后，这里会显示系统会回复给用户的话。</p>
      </div>
      <h3>缺失字段</h3>
      <div class="chips" id="missingSections">
        <span class="chip">等待发送</span>
      </div>
      <h3>当前日报（数据库合并后）</h3>
      <div class="grid">
        <div class="box">
          <h3>today_work</h3>
          <ul id="todayWork"></ul>
        </div>
        <div class="box">
          <h3>problems</h3>
          <ul id="problems"></ul>
        </div>
        <div class="box">
          <h3>tomorrow_plan</h3>
          <ul id="tomorrowPlan"></ul>
        </div>
      </div>
      <div style="height:12px"></div>
      <h3>webhook接收结果 + LLM JSON</h3>
      <pre id="output">{}</pre>
    </section>

    <section>
      <h2>3. 调试信息区</h2>
      <div class="debug">
        <div class="kv"><span>request_id</span><strong id="requestId">-</strong></div>
        <div class="kv"><span>user_id</span><strong id="debugUserId">-</strong></div>
        <div class="kv"><span>是否写入数据库</span><strong id="dbWritten">-</strong></div>
        <div class="kv"><span>日报状态</span><strong id="reportStatus">-</strong></div>
        <div class="kv"><span>完整度</span><strong id="score">-</strong></div>
        <div class="kv"><span>写入日期</span><strong id="writtenDate">-</strong></div>
        <div class="kv"><span>总耗时</span><strong id="totalSeconds">-</strong></div>
        <div class="kv"><span>LLM耗时</span><strong id="llmSeconds">-</strong></div>
      </div>
      <h3>错误日志</h3>
      <pre id="errorLog"></pre>
    </section>
  </main>

  <script>
    const sendBtn = document.getElementById("sendBtn");
    const rawInput = document.getElementById("rawInput");
    const userId = document.getElementById("userId");
    const reportDate = document.getElementById("reportDate");
    const output = document.getElementById("output");
    const errorLog = document.getElementById("errorLog");

    reportDate.value = new Date().toISOString().slice(0, 10);

    function renderList(id, items) {
      const el = document.getElementById(id);
      el.innerHTML = "";
      const list = Array.isArray(items) ? items : [];
      if (list.length === 0) {
        const li = document.createElement("li");
        li.textContent = "空";
        el.appendChild(li);
        return;
      }
      for (const item of list) {
        const li = document.createElement("li");
        li.textContent = item;
        el.appendChild(li);
      }
    }

    function setText(id, value) {
      document.getElementById(id).textContent = value ?? "-";
    }

    const sectionNames = {
      today_work: "今日工作",
      problems: "问题/风险",
      tomorrow_plan: "明日计划"
    };

    function renderMissingSections(items) {
      const el = document.getElementById("missingSections");
      el.innerHTML = "";
      const list = Array.isArray(items) ? items : [];
      if (list.length === 0) {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = "无，三段完整";
        el.appendChild(chip);
        return;
      }
      for (const item of list) {
        const chip = document.createElement("span");
        chip.className = "chip missing";
        chip.textContent = sectionNames[item] || item;
        el.appendChild(chip);
      }
    }

    sendBtn.addEventListener("click", async () => {
      sendBtn.disabled = true;
      sendBtn.textContent = "处理中...";
      output.textContent = "{}";
      errorLog.textContent = "";
      setText("replyText", "处理中...");
      renderMissingSections([]);
      try {
        const resp = await fetch("/debug/send", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            dingtalk_user_id: userId.value.trim(),
            report_date: reportDate.value || null,
            raw_input: rawInput.value.trim()
          })
        });
        const data = await resp.json();
        output.textContent = JSON.stringify({
          webhook_received: data.webhook_received,
          response: data.response,
          steps: data.steps,
          timings: data.timings || {},
          llm_json: data.llm_json || {},
          merged_report: data.merged_report || {
            today_work: data.today_work || [],
            problems: data.problems || [],
            tomorrow_plan: data.tomorrow_plan || []
          }
        }, null, 2);
        renderList("todayWork", data.today_work || data.llm_json?.today_work || []);
        renderList("problems", data.problems_display || data.problems || data.llm_json?.problems || []);
        renderList("tomorrowPlan", data.tomorrow_plan || data.llm_json?.tomorrow_plan || []);
        setText("replyText", data.response?.text?.content || "-");
        renderMissingSections(data.missing_sections || []);
        setText("requestId", data.request_id);
        setText("debugUserId", data.parsed_message?.user_id || userId.value.trim());
        const dbEl = document.getElementById("dbWritten");
        dbEl.textContent = data.db_written ? "是" : "否";
        dbEl.className = data.db_written ? "ok" : "bad";
        setText("reportStatus", data.report_status || "-");
        setText("score", data.completeness_score ?? "-");
        setText("writtenDate", data.report_date || reportDate.value || "-");
        setText("totalSeconds", data.timings?.request_total_seconds ? `${data.timings.request_total_seconds}s` : "-");
        setText("llmSeconds", data.timings?.llm_extract_seconds ? `${data.timings.llm_extract_seconds}s` : "-");
        errorLog.textContent = data.error_log || "无";
      } catch (err) {
        errorLog.textContent = String(err);
      } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = "发送";
      }
    });
  </script>
</body>
</html>
"""
