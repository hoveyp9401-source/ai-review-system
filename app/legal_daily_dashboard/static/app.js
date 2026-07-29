const BASE = "/legal-daily-dashboard";
const TOKEN_KEY = "legalDailyDashboardToken";

const decisions = [
  { value: "normal", label: "正常，无需提醒" },
  { value: "waiting_external", label: "等待外部反馈" },
  { value: "followup", label: "需要跟进" },
  { value: "completed", label: "已完成" },
  { value: "system_error", label: "系统识别错误" },
];

const pageTitles = {
  overview: "今日总览",
  members: "成员日报",
  items: "事项追踪",
  trends: "趋势复盘",
};

const state = {
  token: sessionStorage.getItem(TOKEN_KEY) || "",
  reportDate: localDateString(new Date()),
  team: "",
  overview: null,
  page: "overview",
  actionFilter: "all",
  timelineDays: 14,
  trendPeriod: "week",
  selectedMember: "",
  decisionTarget: null,
  loadingCount: 0,
};

const elements = {
  authGate: document.querySelector("#auth-gate"),
  authForm: document.querySelector("#auth-form"),
  authToken: document.querySelector("#access-token"),
  authError: document.querySelector("#auth-error"),
  appShell: document.querySelector("#app-shell"),
  reportDate: document.querySelector("#report-date"),
  teamFilter: document.querySelector("#team-filter"),
  pageTitle: document.querySelector("#page-title"),
  scopeKicker: document.querySelector("#scope-kicker"),
  loadingBar: document.querySelector("#loading-bar"),
  toast: document.querySelector("#toast"),
  decisionDialog: document.querySelector("#decision-dialog"),
  evidenceDrawer: document.querySelector("#evidence-drawer"),
  drawerBackdrop: document.querySelector("#drawer-backdrop"),
};

function localDateString(value) {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function queryString(values) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined) {
      params.set(key, String(value));
    }
  });
  return params.toString();
}

function setLoading(active) {
  state.loadingCount += active ? 1 : -1;
  state.loadingCount = Math.max(0, state.loadingCount);
  elements.loadingBar.hidden = state.loadingCount === 0;
}

async function api(path, options = {}) {
  setLoading(true);
  try {
    const headers = new Headers(options.headers || {});
    headers.set("X-Legal-Daily-Token", state.token);
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(`${BASE}${path}`, {
      ...options,
      headers,
    });
    if (response.status === 401) {
      signOut("访问凭证无效或已失效，请重新输入。");
      throw new Error("身份验证失败");
    }
    if (!response.ok) {
      let detail = "请求失败，请稍后再试。";
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch (_error) {
        // Keep the safe fallback when the response is not JSON.
      }
      throw new Error(detail);
    }
    const contentType = response.headers.get("content-type") || "";
    return contentType.includes("application/json")
      ? response.json()
      : response;
  } finally {
    setLoading(false);
  }
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 3200);
}

function signOut(message = "") {
  sessionStorage.removeItem(TOKEN_KEY);
  state.token = "";
  state.overview = null;
  elements.appShell.hidden = true;
  elements.authGate.hidden = false;
  elements.authToken.value = "";
  elements.authError.textContent = message;
}

async function bootstrap() {
  elements.reportDate.value = state.reportDate;
  try {
    await loadOverview();
    elements.authGate.hidden = true;
    elements.appShell.hidden = false;
    activatePage("overview");
  } catch (error) {
    if (state.token) {
      elements.authError.textContent = error.message;
    }
  }
}

async function loadOverview() {
  const query = queryString({
    report_date: state.reportDate,
    team: state.team,
  });
  state.overview = await api(`/api/overview?${query}`);
  if (
    state.overview.scope.role === "team_lead" &&
    state.overview.scope.selected_team_ref
  ) {
    state.team = state.overview.scope.selected_team_ref;
  }
  renderTeamOptions();
  renderOverview();
  elements.scopeKicker.textContent =
    state.overview.scope.role === "legal_head"
      ? "法务部门总负责人 · 七团队视角"
      : "团队负责人 · 本团队视角";
}

function renderTeamOptions() {
  const isHead = state.overview.scope.role === "legal_head";
  const options = [];
  if (isHead) {
    options.push('<option value="">全部团队</option>');
  }
  state.overview.teams.forEach((team) => {
    options.push(
      `<option value="${escapeHtml(team.ref)}">${escapeHtml(team.name)}</option>`,
    );
  });
  elements.teamFilter.innerHTML = options.join("");
  elements.teamFilter.value = state.team;
  elements.teamFilter.disabled = !isHead;
}

function metricCard(label, value, note, attention = false) {
  const shown = value === null || value === undefined ? "—" : value;
  return `
    <article class="metric-card ${attention ? "attention" : ""}">
      <span class="metric-label">${escapeHtml(label)}</span>
      <strong class="metric-value">${escapeHtml(shown)}</strong>
      <span class="metric-note">${escapeHtml(note)}</span>
    </article>
  `;
}

function renderOverview() {
  if (!state.overview) return;
  const metrics = state.overview.metrics;
  const metricCards = [
    ["应交人数", metrics.expected_count, "已排除当天无需提交"],
    ["已交人数", metrics.submitted_count, "完成提交的日报"],
    ["尚未提交人数", metrics.outstanding_count, "仅在截止前显示"],
    ["截止后未交人数", metrics.overdue_count, "截止前不显示未交"],
    ["未最终确认人数", metrics.not_finally_confirmed_count, "含自动提交"],
    [
      "建议管理者关注人数",
      metrics.attention_member_count,
      `${metrics.review_suggested_count} 条可追溯提示`,
    ],
    ["长期未推进事项", metrics.stalled_item_count, "不以重复直接定性"],
  ];
  document.querySelector("#overview-metrics").innerHTML = metricCards
    .map((card, index) => metricCard(...card, index >= 3))
    .join("");

  const notice = document.querySelector("#data-notice");
  const messages = state.overview.data_quality.messages || [];
  notice.hidden = messages.length === 0;
  notice.textContent = messages.join(" ");
  document.querySelector("#overview-as-of").textContent =
    `数据截至 ${formatDateTime(state.overview.as_of)}`;
  document.querySelector("#support-summary").textContent =
    state.overview.management_summary.headline;
  renderActions();
  renderTeamComparison();
}

function renderActions() {
  const container = document.querySelector("#action-list");
  const actions = (state.overview.actions || []).filter(
    (action) =>
      state.actionFilter === "all" ||
      action.category === state.actionFilter,
  );
  if (!actions.length) {
    container.innerHTML =
      '<div class="empty-inline">当前筛选下没有待负责人处理的事项。</div>';
    return;
  }
  container.innerHTML = actions
    .map(
      (action) => `
        <article class="action-card">
          <div>
            <span class="tag ${
              action.category === "department_support" ||
              action.category === "today_confirmation"
                ? "support"
                : ""
            }">
              ${escapeHtml(action.category_label)}
            </span>
            <h3>${escapeHtml(action.title)}</h3>
            <span class="action-meta">
              ${escapeHtml(action.team_name)}
              ${action.member_name ? ` · ${escapeHtml(action.member_name)}` : ""}
            </span>
          </div>
          <div>
            <p class="action-reason">${escapeHtml(action.reason)}</p>
            <p class="action-support">需要支撑：${escapeHtml(action.support_needed || "请负责人结合原文判断。")}</p>
            <p class="tiny-meta">
              ${
                action.model_version
                  ? `分析版本 ${escapeHtml(action.model_version)}`
                  : "客观提交事实"
              }
              ${action.evaluated_at ? ` · 更新 ${escapeHtml(formatDateTime(action.evaluated_at))}` : ""}
            </p>
            ${renderDecisionHistory(action.manager_decision)}
          </div>
          <div class="action-controls">
            <button class="button quiet small" data-evidence-action="${escapeHtml(action.target_ref)}">
              查看证据
            </button>
            <span class="status-chip">${escapeHtml(action.manager_status)}</span>
            ${
              state.overview.capabilities.manager_decision_write &&
              action.can_record_decision
                ? `<button
                    class="button primary small"
                    data-decision-type="${escapeHtml(action.target_type)}"
                    data-decision-ref="${escapeHtml(action.target_ref)}"
                    data-decision-title="${escapeHtml(action.title)}"
                  >处理</button>`
                : ""
            }
          </div>
        </article>
      `,
    )
    .join("");

  container.querySelectorAll("[data-evidence-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = state.overview.actions.find(
        (item) => item.target_ref === button.dataset.evidenceAction,
      );
      showEvidence({
        title: action.title,
        reason: action.reason,
        evidence: action.evidence,
        facts: action.facts,
        comparedDates: action.compared_dates,
        confidence: action.confidence,
        modelVersion: action.model_version,
        evaluatedAt: action.evaluated_at,
        managerDecision: action.manager_decision,
      });
    });
  });
  bindDecisionButtons(container);
}

function renderTeamComparison() {
  const body = document.querySelector("#team-comparison-body");
  body.innerHTML = state.overview.teams
    .map((team) => {
      const metrics = team.metrics || {};
      return `
        <tr data-team-ref="${escapeHtml(team.ref)}">
          <td class="team-name">${escapeHtml(team.name)}</td>
          <td>${formatMetric(metrics.expected_count)}</td>
          <td>${formatMetric(metrics.submitted_count)}</td>
          <td class="${attentionClass(metrics.outstanding_count)}">${formatMetric(metrics.outstanding_count)}</td>
          <td class="${attentionClass(metrics.overdue_count)}">${formatMetric(metrics.overdue_count)}</td>
          <td class="${attentionClass(metrics.not_finally_confirmed_count)}">${formatMetric(metrics.not_finally_confirmed_count)}</td>
          <td class="${attentionClass(metrics.review_suggested_count)}">${formatMetric(metrics.review_suggested_count)}</td>
          <td>›</td>
        </tr>
      `;
    })
    .join("");
  body.querySelectorAll("[data-team-ref]").forEach((row) => {
    row.addEventListener("click", async () => {
      state.team = row.dataset.teamRef;
      elements.teamFilter.value = state.team;
      state.selectedMember = "";
      await navigate("members");
    });
  });
}

function formatMetric(value) {
  return value === null || value === undefined ? "待核实" : escapeHtml(value);
}

function attentionClass(value) {
  return typeof value === "number" && value > 0 ? "number-attention" : "";
}

function renderDecisionHistory(decision) {
  if (!decision) return "";
  const note = decision.note
    ? ` · 说明：${escapeHtml(decision.note)}`
    : "";
  return `
    <p class="decision-history">
      最近处理：${escapeHtml(decision.actor_name || "负责人")}
      · ${escapeHtml(decision.decision_label)}
      · ${escapeHtml(formatDateTime(decision.created_at))}
      ${note}
    </p>
  `;
}

async function loadMembers() {
  const query = queryString({
    report_date: state.reportDate,
    team: state.team,
  });
  const result = await api(`/api/members?${query}`);
  renderMembers(result.members || []);
  if (state.selectedMember) {
    const selected = result.members.find(
      (member) => member.ref === state.selectedMember,
    );
    if (selected) {
      await loadTimeline(state.selectedMember);
    } else {
      clearTimeline();
    }
  }
}

function renderMembers(members) {
  document.querySelector("#member-count").textContent = `${members.length} 人`;
  const container = document.querySelector("#member-list");
  if (!members.length) {
    container.innerHTML =
      '<div class="empty-inline">当前范围内没有成员数据。</div>';
    clearTimeline();
    return;
  }
  container.innerHTML = members
    .map(
      (member) => `
        <button
          class="member-row ${state.selectedMember === member.ref ? "active" : ""}"
          data-member-ref="${escapeHtml(member.ref)}"
        >
          <span class="avatar">${escapeHtml(member.name.slice(0, 1))}</span>
          <span class="member-copy">
            <strong>${escapeHtml(member.name)}</strong>
            <span>
              ${escapeHtml(member.status_label)}
              ${member.suggest_review ? " · 建议复核" : ""}
            </span>
          </span>
        </button>
      `,
    )
    .join("");
  container.querySelectorAll("[data-member-ref]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.selectedMember = button.dataset.memberRef;
      container
        .querySelectorAll(".member-row")
        .forEach((row) => row.classList.remove("active"));
      button.classList.add("active");
      await loadTimeline(state.selectedMember);
    });
  });
}

function clearTimeline() {
  state.selectedMember = "";
  document.querySelector("#timeline-empty").hidden = false;
  document.querySelector("#member-timeline").hidden = true;
}

async function loadTimeline(memberRef) {
  const query = queryString({
    end_date: state.reportDate,
    days: state.timelineDays,
  });
  const result = await api(
    `/api/members/${encodeURIComponent(memberRef)}/timeline?${query}`,
  );
  renderTimeline(result);
}

function renderTimeline(result) {
  const container = document.querySelector("#member-timeline");
  document.querySelector("#timeline-empty").hidden = true;
  container.hidden = false;
  container.innerHTML = `
    <header class="timeline-header">
      <div>
        <p class="eyebrow">PERSONAL DAILY LINE</p>
        <h2>${escapeHtml(result.member.name)}</h2>
        <p>${escapeHtml(result.member.team_name)} · ${escapeHtml(result.range.start_date)} 至 ${escapeHtml(result.range.end_date)}</p>
      </div>
      ${
        state.overview.capabilities.analysis_refresh
          ? `<button class="button quiet" id="refresh-member-analysis" type="button">
              重新分析近 ${escapeHtml(state.timelineDays)} 天
            </button>`
          : ""
      }
    </header>
    <div class="timeline-axis">
      ${result.days.map(renderTimelineDay).join("")}
    </div>
  `;
  const refreshButton = container.querySelector("#refresh-member-analysis");
  if (refreshButton) {
    refreshButton.addEventListener("click", async () => {
      await refreshMemberAnalysis(state.selectedMember);
    });
  }
  container.querySelectorAll("[data-day-evidence]").forEach((button) => {
    button.addEventListener("click", () => {
      const day = result.days.find(
        (item) => item.date === button.dataset.dayEvidence,
      );
      showEvidence({
        title: `${result.member.name} · ${day.date_label}`,
        reason: "当天日报原文",
        evidence: [
          {
            date: day.date,
            section: "日报原文",
            quote: day.raw_evidence || "当天没有可展示的原文。",
          },
        ],
      });
    });
  });
  container.querySelectorAll("[data-suggestion-ref]").forEach((button) => {
    button.addEventListener("click", () => {
      for (const day of result.days) {
        const suggestion = day.review_suggestions.find(
          (item) => item.ref === button.dataset.suggestionRef,
        );
        if (suggestion) {
          showEvidence({
            title: `${result.member.name} · 建议复核`,
            reason: suggestion.reason,
            evidence: suggestion.evidence,
            comparedDates: suggestion.compared_dates,
            confidence: suggestion.confidence,
            modelVersion: suggestion.model_version,
            evaluatedAt: suggestion.evaluated_at,
            managerDecision: suggestion.manager_decision,
          });
          break;
        }
      }
    });
  });
  bindDecisionButtons(container);
}

async function refreshMemberAnalysis(memberRef) {
  if (!memberRef) {
    showToast("请先选择一位成员。");
    return;
  }
  const query = queryString({
    end_date: state.reportDate,
    days: state.timelineDays,
  });
  try {
    const result = await api(
      `/api/members/${encodeURIComponent(memberRef)}/analysis?${query}`,
      { method: "POST" },
    );
    showToast(
      `分析已更新：${result.suggestion_count} 条复核建议，${result.work_item_count} 个事项。`,
    );
    await loadOverview();
    await loadTimeline(memberRef);
  } catch (error) {
    showToast(error.message);
  }
}

function renderTimelineDay(day) {
  const hasReport =
    day.today_work.length ||
    day.problems.length ||
    day.tomorrow_plan.length ||
    day.raw_evidence;
  return `
    <article class="timeline-day ${hasReport ? "" : "missing"}">
      <div class="day-head">
        <h3>${escapeHtml(day.date_label)}</h3>
        <span class="status-chip ${day.status_label.includes("未") ? "attention" : ""}">
          ${escapeHtml(day.status_label)}
        </span>
      </div>
      <div class="day-card">
        <div class="report-columns">
          ${renderReportSection("完成工作", day.today_work)}
          ${renderReportSection("问题风险", day.problems)}
          ${renderReportSection("明日计划", day.tomorrow_plan)}
        </div>
        <div class="day-footer">
          ${
            day.submitted_at
              ? `<span class="tiny-meta">提交 ${escapeHtml(formatDateTime(day.submitted_at))}</span>`
              : ""
          }
          <span class="tag neutral">${escapeHtml(day.confirmation_label)}</span>
          ${
            day.raw_evidence
              ? `<button class="button quiet small" data-day-evidence="${escapeHtml(day.date)}">查看当天原文</button>`
              : ""
          }
          ${
            day.section_completeness?.known &&
            !day.section_completeness.complete
              ? `<span class="status-chip attention">栏目缺失：${day.section_completeness.missing_sections.map(escapeHtml).join("、")}</span>`
              : ""
          }
        </div>
        ${day.review_suggestions
          .map(
            (suggestion) => `
              <div class="review-callout">
                <strong>建议复核 · ${escapeHtml(suggestion.manager_status)}</strong>
                ${escapeHtml(suggestion.reason)}
                <div class="day-footer">
                  <button class="button quiet small" data-suggestion-ref="${escapeHtml(suggestion.ref)}">查看比较证据</button>
                  ${
                    state.overview.capabilities.manager_decision_write
                      ? `<button
                          class="button primary small"
                          data-decision-type="review_suggestion"
                          data-decision-ref="${escapeHtml(suggestion.ref)}"
                          data-decision-title="${escapeHtml(day.date_label)}的复核建议"
                        >处理</button>`
                      : ""
                  }
                </div>
              </div>
            `,
          )
          .join("")}
        ${day.linked_items
          .map(
            (item) => `
              <div class="linked-item">
                <strong>关联事项 · ${escapeHtml(item.status_label)}</strong>
                ${escapeHtml(item.title)}：${escapeHtml(item.summary)}
                <span class="tiny-meta"> · ${escapeHtml(item.manager_status)}</span>
              </div>
            `,
          )
          .join("")}
      </div>
    </article>
  `;
}

function renderReportSection(title, values) {
  return `
    <section class="report-section">
      <h4>${escapeHtml(title)}</h4>
      ${
        values.length
          ? `<ul>${values.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>`
          : '<span class="blank">暂无内容</span>'
      }
    </section>
  `;
}

async function loadItems() {
  const query = queryString({
    end_date: state.reportDate,
    team: state.team,
  });
  const result = await api(`/api/items?${query}`);
  renderItems(result.items || []);
}

function renderItems(items) {
  const statuses = [...new Set(items.map((item) => item.status_label))];
  document.querySelector("#item-legend").innerHTML = statuses
    .map((status) => `<span class="tag neutral">${escapeHtml(status)}</span>`)
    .join("");
  const container = document.querySelector("#item-board");
  if (!items.length) {
    container.innerHTML =
      '<div class="empty-state"><div><h3>暂无事项记录</h3><p>当前团队和日期范围内，没有可展示的长期事项。</p></div></div>';
    return;
  }
  container.innerHTML = items
    .map(
      (item) => `
        <article class="item-card">
          <div class="item-card-head">
            <div>
              <span class="tag ${item.status === "waiting_external" ? "" : "support"}">${escapeHtml(item.status_label)}</span>
              <h3>${escapeHtml(item.title)}</h3>
              <span class="tiny-meta">${escapeHtml(item.team_name)} · ${escapeHtml(item.member_names.join("、"))}</span>
            </div>
            <span class="status-chip">${escapeHtml(item.manager_status)}</span>
          </div>
          <p class="item-summary">${escapeHtml(item.summary)}</p>
          <p class="tiny-meta">
            ${escapeHtml(item.first_seen)} 至 ${escapeHtml(item.last_seen)}
            · 系统把握 ${formatConfidence(item.confidence)}
            ${item.model_version ? ` · 分析版本 ${escapeHtml(item.model_version)}` : ""}
            ${item.evaluated_at ? ` · 更新 ${escapeHtml(formatDateTime(item.evaluated_at))}` : ""}
          </p>
          ${renderDecisionHistory(item.manager_decision)}
          <div class="item-timeline">
            ${item.timeline
              .slice(-4)
              .map(
                (entry) => `
                  <div class="item-entry">
                    <time>${escapeHtml(entry.date)}</time>
                    <blockquote>${escapeHtml(entry.quote)}</blockquote>
                  </div>
                `,
              )
              .join("")}
          </div>
          <div class="day-footer">
            <button class="button quiet small" data-item-evidence="${escapeHtml(item.ref)}">查看完整时间线</button>
            ${
              state.overview.capabilities.manager_decision_write
                ? `<button
                    class="button primary small"
                    data-decision-type="work_item"
                    data-decision-ref="${escapeHtml(item.ref)}"
                    data-decision-title="${escapeHtml(item.title)}"
                  >标记事项</button>`
                : ""
            }
          </div>
        </article>
      `,
    )
    .join("");
  container.querySelectorAll("[data-item-evidence]").forEach((button) => {
    button.addEventListener("click", () => {
      const item = items.find(
        (entry) => entry.ref === button.dataset.itemEvidence,
      );
      showEvidence({
        title: item.title,
        reason: `${item.status_label}。${item.judgement_boundary}`,
        confidence: item.confidence,
        modelVersion: item.model_version,
        evaluatedAt: item.evaluated_at,
        managerDecision: item.manager_decision,
        evidence: item.timeline.map((entry) => ({
          date: entry.date,
          section: `${entry.member_name} · ${entry.section}`,
          quote: entry.quote,
        })),
      });
    });
  });
  bindDecisionButtons(container);
}

async function loadTrends() {
  const query = queryString({
    end_date: state.reportDate,
    period: state.trendPeriod,
    team: state.team,
  });
  const result = await api(`/api/trends?${query}`);
  renderTrends(result);
}

function renderTrends(result) {
  document.querySelector("#trend-range-label").textContent =
    `${result.period.start_date} 至 ${result.period.end_date}`;
  const teams = document.querySelector("#trend-teams");
  teams.innerHTML = result.teams
    .map((team) => {
      const expected = team.expected_count;
      const ratio =
        typeof expected === "number" && expected > 0
          ? Math.min(100, Math.round((team.submitted_count / expected) * 100))
          : 0;
      return `
        <div class="trend-team-row">
          <strong>${escapeHtml(team.team_name)}</strong>
          <div class="progress-track"><div class="progress-fill" style="width:${ratio}%"></div></div>
          <span>${escapeHtml(team.submitted_count)} / ${expected === null ? "待核实" : escapeHtml(expected)}</span>
        </div>
      `;
    })
    .join("");
  teams.insertAdjacentHTML(
    "beforeend",
    `
      <div class="editorial-list">
        ${(result.unsubmitted_trend || [])
          .map(
            (point) => `
              <div class="editorial-row">
                <strong>${escapeHtml(point.date)} · 未交 ${formatMetric(point.overdue_count)} · 延迟提交 ${formatMetric(point.late_submitted_count)} · 尚未提交 ${formatMetric(point.outstanding_count)}</strong>
              </div>
            `,
          )
          .join("")}
      </div>
    `,
  );
  renderEditorialList(
    "#recurring-problems",
    result.recurring_problems,
    (item) => ({
      title: `${item.label} · ${item.count} 次`,
      body: "仅作为复核线索，不形成员工评价。",
    }),
  );
  renderEditorialList(
    "#coordination-needed",
    result.coordination_needed,
    (item) => ({
      title: `${item.team_name} · ${item.title}`,
      body: `${item.support_needed} 原因：${item.reason}`,
    }),
  );
  renderEditorialList(
    "#long-running-items",
    result.long_running_items,
    (item) => ({
      title: `${item.team_name} · ${item.title} · ${item.status_label}`,
      body: item.summary,
    }),
  );
  const focusTarget = document.querySelector("#recent-focus");
  if (focusTarget) {
    renderEditorialList(
      "#recent-focus",
      result.recent_focus,
      (item) => ({
        title: `${item.team_name} · ${item.title}`,
        body: item.status_label,
      }),
    );
  }
}

function renderEditorialList(selector, items, toCopy) {
  const container = document.querySelector(selector);
  if (!container) return;
  if (!items || !items.length) {
    container.innerHTML = '<div class="empty-inline">当前没有相关事项。</div>';
    return;
  }
  container.innerHTML = `
    <div class="editorial-list">
      ${items
        .map((item) => {
          const copy = toCopy(item);
          return `
            <div class="editorial-row">
              <strong>${escapeHtml(copy.title)}</strong>
              <p>${escapeHtml(copy.body)}</p>
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

async function downloadBriefing() {
  const query = queryString({
    end_date: state.reportDate,
    period: state.trendPeriod,
    team: state.team,
  });
  try {
    const response = await api(`/api/briefing?${query}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `法务日报负责人简报-${state.reportDate}.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast("负责人简报已导出。");
  } catch (error) {
    showToast(error.message);
  }
}

function showEvidence({
  title,
  reason,
  evidence = [],
  facts = [],
  comparedDates = [],
  confidence,
  modelVersion,
  evaluatedAt,
  managerDecision,
}) {
  document.querySelector("#evidence-title").textContent = title || "原文证据";
  document.querySelector("#evidence-content").innerHTML = `
    <div class="drawer-summary">
      <strong>系统为什么提示</strong><br />
      ${escapeHtml(reason || "请结合原文判断实际进展。")}
    </div>
    ${
      comparedDates.length
        ? `<p class="confidence-note">比较日期：${comparedDates.map(escapeHtml).join("、")}</p>`
        : ""
    }
    ${
      confidence !== undefined && confidence !== null
        ? `<p class="confidence-note">系统判断把握：${formatConfidence(confidence)}。该数值表示模型把握，不是员工评分。</p>`
        : ""
    }
    ${
      modelVersion
        ? `<p class="confidence-note">分析版本：${escapeHtml(modelVersion)}${evaluatedAt ? ` · 最后评估：${escapeHtml(formatDateTime(evaluatedAt))}` : ""}</p>`
        : evaluatedAt
          ? `<p class="confidence-note">事实核对时间：${escapeHtml(formatDateTime(evaluatedAt))}</p>`
          : ""
    }
    ${
      facts.length
        ? `<div class="fact-list">${facts
            .map(
              (fact) => `
                <div><span>${escapeHtml(fact.label)}</span><strong>${escapeHtml(fact.value)}</strong></div>
              `,
            )
            .join("")}</div>`
        : ""
    }
    ${renderDecisionHistory(managerDecision)}
    ${evidence
      .map(
        (entry) => `
          <article class="evidence-quote">
            <header>
              <span>${escapeHtml(entry.date)}</span>
              <span>${escapeHtml(entry.section)}</span>
            </header>
            <blockquote>${escapeHtml(entry.quote)}</blockquote>
          </article>
        `,
      )
      .join("")}
    ${
      evidence.length
        ? ""
        : '<div class="empty-inline">当前没有可展示的原文证据。</div>'
    }
  `;
  elements.evidenceDrawer.classList.add("open");
  elements.evidenceDrawer.setAttribute("aria-hidden", "false");
  elements.drawerBackdrop.hidden = false;
}

function closeEvidence() {
  elements.evidenceDrawer.classList.remove("open");
  elements.evidenceDrawer.setAttribute("aria-hidden", "true");
  elements.drawerBackdrop.hidden = true;
}

function bindDecisionButtons(container) {
  container.querySelectorAll("[data-decision-ref]").forEach((button) => {
    button.addEventListener("click", () => {
      openDecision({
        type: button.dataset.decisionType,
        ref: button.dataset.decisionRef,
        title: button.dataset.decisionTitle,
      });
    });
  });
}

function openDecision(target) {
  state.decisionTarget = {
    ...target,
    idempotencyKey:
      window.crypto?.randomUUID?.() ||
      `decision-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  };
  document.querySelector("#decision-target-title").textContent = target.title;
  document.querySelector("#decision-options").innerHTML = decisions
    .map(
      (decision, index) => `
        <label class="decision-option">
          <input
            type="radio"
            name="manager-decision"
            value="${decision.value}"
            ${index === 2 ? "checked" : ""}
          />
          <span>${escapeHtml(decision.label)}</span>
        </label>
      `,
    )
    .join("");
  document.querySelector("#decision-note").value = "";
  elements.decisionDialog.showModal();
}

async function saveDecision(event) {
  event.preventDefault();
  if (!state.decisionTarget) return;
  const selected = document.querySelector(
    'input[name="manager-decision"]:checked',
  );
  if (!selected) return;
  try {
    await api("/api/manager-decisions", {
      method: "POST",
      body: JSON.stringify({
        target_type: state.decisionTarget.type,
        target_ref: state.decisionTarget.ref,
        decision: selected.value,
        note: document.querySelector("#decision-note").value.trim(),
        idempotency_key: state.decisionTarget.idempotencyKey,
      }),
    });
    elements.decisionDialog.close();
    showToast("负责人处理结果已保存，员工日报原文未被修改。");
    await loadOverview();
    if (state.page === "members" && state.selectedMember) {
      await loadTimeline(state.selectedMember);
    }
    if (state.page === "items") {
      await loadItems();
    }
  } catch (error) {
    showToast(error.message);
  }
}

async function navigate(page) {
  activatePage(page);
  try {
    if (page === "overview") {
      await loadOverview();
    } else if (page === "members") {
      await loadMembers();
    } else if (page === "items") {
      await loadItems();
    } else if (page === "trends") {
      await loadTrends();
    }
  } catch (error) {
    showToast(error.message);
  }
}

function activatePage(page) {
  state.page = page;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === page);
  });
  document.querySelectorAll(".page").forEach((section) => {
    section.classList.toggle("active", section.id === `page-${page}`);
  });
  elements.pageTitle.textContent = pageTitles[page];
}

function formatDateTime(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function formatConfidence(value) {
  if (typeof value !== "number") return "待核实";
  return `${Math.round(value * 100)}%`;
}

elements.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = elements.authToken.value.trim();
  if (!token) return;
  state.token = token;
  sessionStorage.setItem(TOKEN_KEY, token);
  elements.authError.textContent = "";
  await bootstrap();
});

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => navigate(button.dataset.page));
});

document.querySelector("#refresh").addEventListener("click", () => {
  navigate(state.page);
});

elements.reportDate.addEventListener("change", async () => {
  state.reportDate = elements.reportDate.value;
  state.selectedMember = "";
  await loadOverview();
  if (state.page !== "overview") {
    await navigate(state.page);
  }
});

elements.teamFilter.addEventListener("change", async () => {
  state.team = elements.teamFilter.value;
  state.selectedMember = "";
  await loadOverview();
  if (state.page !== "overview") {
    await navigate(state.page);
  }
});

document.querySelectorAll("[data-action-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.actionFilter = button.dataset.actionFilter;
    document
      .querySelectorAll("[data-action-filter]")
      .forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    renderActions();
  });
});

document.querySelectorAll("[data-days]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.timelineDays = Number(button.dataset.days);
    document
      .querySelectorAll("[data-days]")
      .forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    if (state.selectedMember) {
      await loadTimeline(state.selectedMember);
    }
  });
});

document.querySelectorAll("[data-period]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.trendPeriod = button.dataset.period;
    document
      .querySelectorAll("[data-period]")
      .forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    await loadTrends();
  });
});

document
  .querySelector("#download-briefing")
  .addEventListener("click", downloadBriefing);
document
  .querySelector("#close-evidence")
  .addEventListener("click", closeEvidence);
elements.drawerBackdrop.addEventListener("click", closeEvidence);
document
  .querySelector("#decision-form")
  .addEventListener("submit", saveDecision);
document.querySelector("#cancel-decision").addEventListener("click", () => {
  elements.decisionDialog.close();
});
document.querySelector("#close-decision").addEventListener("click", () => {
  elements.decisionDialog.close();
});
document.querySelector("#sign-out").addEventListener("click", () => signOut());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeEvidence();
});

if (state.token) {
  bootstrap();
}
