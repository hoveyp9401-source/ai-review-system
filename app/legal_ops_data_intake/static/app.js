const state = {
  token: sessionStorage.getItem("legalOpsCredential") || "",
  shell: null,
  page: new URLSearchParams(location.search).get("page") || "performance",
  selectedRule: new URLSearchParams(location.search).get("rule") || "",
  selectedDimension: new URLSearchParams(location.search).get("dimension") || "",
  dashboard: null,
  periods: [],
  rules: [],
  batches: [],
  errors: [],
  lastPreviewBatch: "",
  selectedPeriod: "",
  workspaceTab: "",
  metricPreviews: new Map(),
  performanceReportView: new URLSearchParams(location.search).get("view") === "week" ? "week" : "month",
  performanceReportAnchorDate: new URLSearchParams(location.search).get("anchor_date") || "",
  performanceReportScopeKey: new URLSearchParams(location.search).get("scope") || "__total__",
  performanceReportDetailTab: "branches",
  pendingSourceMappings: new Map(),
};

const $ = (selector) => document.querySelector(selector);
const content = $("#content");
const navigation = $("#navigation");
const loginModal = $("#login-modal");
const loginForm = $("#login-form");
const loginError = $("#login-error");
const drawer = $("#drawer");
const drawerMask = $("#drawer-mask");
const drawerContent = $("#drawer-content");

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

const ruleText = (value, fallback = "—") => {
  if (Array.isArray(value)) {
    const items = value.map(item => ruleText(item, "")).filter(Boolean);
    return items.length ? items.join("、") : fallback;
  }
  if (value && typeof value === "object") {
    for (const key of ["name", "label", "key", "description", "issue", "message"]) {
      if (value[key] !== undefined && value[key] !== null) {
        const selected = ruleText(value[key], "");
        if (selected) return selected;
      }
    }
    return fallback;
  }
  if (value === undefined || value === null || !String(value).trim()) return fallback;
  return String(value);
};

const ruleValue = (item, keys, fallback = "—") => {
  for (const key of keys) {
    const value = item?.[key];
    if (value !== undefined && value !== null) {
      const selected = ruleText(value, "");
      if (selected) return selected;
    }
  }
  return fallback;
};

const businessMetricName = value => {
  const text = ruleText(value, "");
  if (!text) return "指标名称待确认";
  return [...text].some(character => character.codePointAt(0) > 127)
    ? text
    : "指标名称待确认";
};

const metricDisplayText = metric => String(
  metric?.display_text
  || `${metric?.value ?? "—"}${metric?.target_unit || metric?.unit || ""}`,
);

const columnTypeLabel = value => ({
  text: "文本",
  date: "日期",
  decimal: "数值",
  percentage: "比例",
  integer: "整数",
  unknown: "待识别",
}[String(value || "").toLowerCase()] || "待识别");

const lineageSourceText = entry => {
  const ranges = (entry?.source_ranges || []).map(source => {
    const parts = (source.ranges || []).map(([start, end]) => start === end ? `${start}` : `${start}-${end}`);
    return parts.length
      ? `${source.table_name || "源表"}第 ${parts.join("、")} 行（共 ${source.row_count || 0} 条）`
      : "";
  }).filter(Boolean);
  if (ranges.length) return ranges.join("；");
  const rows = (entry?.source_rows || []).map(row => `${row.table_name || "源表"}第 ${row.row_number || "未记录"} 行`);
  return rows.length ? rows.join("、") : "规则常量";
};

const lineageMetricText = entry => (entry?.metrics || [])
  .map(metric => `${metric.name}＝${metric.value}（${metric.source_row_count || 0}条）`)
  .join("；");

const formatTime = (value) => {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(parsed);
};

const statusClass = (status) => {
  if (["published", "active", "valid", "ready", "achieved"].includes(status)) return "good";
  if (["validation_failed", "error", "failed", "conflict", "not_achieved"].includes(status)) return "bad";
  if (["uploaded", "understanding_draft", "awaiting_confirmation", "warning", "missing"].includes(status)) return "warn";
  return "info";
};

const canAbandonBatch = (item) => {
  if (!item.can_abandon) return false;
  const capabilities = state.shell?.identity?.capabilities || {};
  if (capabilities.publish) return true;
  if (["performance_rule_package", "performance_source_table"].includes(item.business_type)) {
    return Boolean(capabilities.performance_upload);
  }
  return Boolean(capabilities.case_upload);
};

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 3600);
}

function errorMessage(payload, fallback) {
  if (typeof payload?.detail === "string") return payload.detail;
  return payload?.detail?.message || payload?.message || fallback;
}

async function api(path, options = {}) {
  const response = await fetch(`/legal-ops/data-intake/api/${path}`, {
    ...options,
    headers: {
      "X-Legal-Ops-Token": state.token,
      ...(options.body instanceof FormData ? {} : options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    sessionStorage.removeItem("legalOpsCredential");
    loginModal.classList.remove("hidden");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(errorMessage(payload, `操作失败（${response.status}）`));
  }
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) return response.json();
  return response.blob();
}

function setBusy(button, busy, text = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.previousText = button.textContent;
    button.textContent = text;
    button.disabled = true;
    button.classList.add("loading");
  } else {
    button.textContent = button.dataset.previousText || button.textContent;
    button.disabled = false;
    button.classList.remove("loading");
  }
}

async function mutate(button, operation, successMessage, refresh = true) {
  setBusy(button, true);
  try {
    const result = await operation();
    showToast(successMessage || result.status_label || "操作成功");
    if (refresh) await loadPage();
    return result;
  } catch (error) {
    showToast(error.message, true);
    throw error;
  } finally {
    setBusy(button, false);
  }
}

async function download(path, filename, button) {
  setBusy(button, true, "准备下载…");
  try {
    const blob = await api(path);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = filename;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    URL.revokeObjectURL(url);
    showToast("文件已开始下载");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

const pageTitles = {
  performance: ["绩效板块", "选择板块后，集中维护 Skill、底表和指标"],
  "case-master": ["案件主表导入", "维护 ERP 权威案件字段，不删除本地信息"],
  "case-progress": ["案件进展导入", "先匹配案件，再写入文件来源时间线"],
  batches: ["导入批次记录", "查看每一次上传、校验、发布和操作人"],
  errors: ["导入错误处理", "集中处理无法匹配、格式错误和数据冲突"],
};

function renderNavigation() {
  navigation.innerHTML = (state.shell?.navigation || []).map(item => `
    <button class="nav-item ${state.page === item.code ? "active" : ""}" data-page="${item.code}" type="button">
      ${escapeHtml(item.code === "performance" ? "绩效板块" : item.label)}
    </button>`).join("");
  navigation.querySelectorAll("[data-page]").forEach(button => {
    button.addEventListener("click", () => {
      state.page = button.dataset.page;
      state.selectedRule = "";
      state.selectedDimension = "";
      history.replaceState({}, "", `?page=${encodeURIComponent(state.page)}`);
      renderNavigation();
      loadPage();
    });
  });
}

async function bootstrap() {
  if (!state.token) {
    loginModal.classList.remove("hidden");
    return;
  }
  try {
    state.shell = await api("shell");
    loginModal.classList.add("hidden");
    $("#safety-notice").textContent = state.shell.safety_notice;
    $("#identity").textContent = `${state.shell.identity.user_name} · ${state.shell.identity.roles.map(r => r.label).join(" / ")}`;
    renderNavigation();
    await loadPage();
  } catch (error) {
    loginError.textContent = error.message;
  }
}

async function loadPage() {
  const [title, subtitle] = pageTitles[state.page] || pageTitles.performance;
  $("#page-title").textContent = title;
  $("#page-eyebrow").textContent = subtitle;
  content.innerHTML = `<div class="loading"><span></span>正在加载</div>`;
  try {
    if (state.page === "performance") await loadPerformance();
    else if (state.page === "case-master") await loadCaseImport("case-master");
    else if (state.page === "case-progress") await loadCaseImport("case-progress");
    else if (state.page === "batches") await loadBatches();
    else if (state.page === "errors") await loadErrors();
  } catch (error) {
    content.innerHTML = `<div class="empty"><div><strong>页面暂时无法加载</strong>${escapeHtml(error.message)}</div></div>`;
    showToast(error.message, true);
  }
}

async function loadPerformance() {
  [state.dashboard, state.periods, state.rules] = await Promise.all([
    api("dashboard"), api("periods"), api("rules"),
  ]);
  if (!state.selectedPeriod && state.periods[0]) {
    state.selectedPeriod = state.periods[0].period_ref;
  }
  if (state.selectedRule || state.selectedDimension) {
    await loadPerformanceBoardWorkspace(state.selectedRule);
    return;
  }
  content.innerHTML = renderPerformanceBoardHome();
  bindPerformanceBoardHome();
}

const PERFORMANCE_DIMENSIONS = [
  "被告案件",
  "索赔管理",
  "非诉收款",
  "诉讼案件收款",
  "诉讼利息收入",
  "未审定诉讼结算增加额",
];

const performanceDimensionKey = value => String(value || "")
  .replace(/[\s\-_/\\:：,，。；;（）()【】[\]]+/g, "")
  .toLowerCase();

function performanceBoards() {
  const usedRules = new Set();
  const boards = PERFORMANCE_DIMENSIONS.map(name => {
    const target = performanceDimensionKey(name);
    const rule = state.rules.find(item => {
      const scope = performanceDimensionKey(item.business_scope_name);
      const skill = performanceDimensionKey(item.skill_name);
      return scope === target || scope.includes(target) || skill.includes(target);
    }) || null;
    if (rule) usedRules.add(rule.rule_ref);
    return { name, rule };
  });
  const seenScopes = new Set(boards.map(item => performanceDimensionKey(item.name)));
  for (const rule of state.rules) {
    if (usedRules.has(rule.rule_ref)) continue;
    const name = rule.business_scope_name === "尚未填写"
      ? rule.skill_name
      : rule.business_scope_name;
    const key = performanceDimensionKey(name);
    if (!key || seenScopes.has(key)) continue;
    boards.push({ name, rule });
    seenScopes.add(key);
  }
  return boards;
}

function renderPerformanceBoardHome() {
  const caps = state.shell.identity.capabilities;
  const boards = performanceBoards();
  return `
    <section class="board-hero">
      <div>
        <p class="eyebrow">绩效数据维护</p>
        <h2>选择要维护的板块</h2>
        <p>每个板块单独管理自己的 Skill、底表和指标。底表按现有格式直接上传，不需要改成系统模板。</p>
      </div>
      ${caps.performance_upload ? `<button class="button primary" id="new-board-toggle" type="button">新增绩效板块</button>` : ""}
    </section>
    <section class="board-grid" aria-label="绩效板块">
      ${boards.map(({ name, rule }) => `
        <button class="board-card" type="button"
          data-rule="${escapeHtml(rule?.rule_ref || "")}"
          data-dimension="${escapeHtml(name)}">
          <span class="board-card-top">
            <span class="board-kicker">绩效板块</span>
            <span class="badge ${rule ? statusClass(rule.status) : "info"}">${rule ? escapeHtml(rule.status_label) : "待配置"}</span>
          </span>
          <strong>${escapeHtml(name)}</strong>
          <span class="board-description">${rule
            ? `${escapeHtml(rule.skill_name)} · ${rule.unresolved?.length || 0} 项待确认`
            : "尚未上传 Skill.md，可进入后开始配置"}</span>
          <span class="board-enter">${rule ? "进入维护" : "开始配置"} →</span>
        </button>`).join("")}
    </section>
    <section class="card board-create-panel hidden" id="new-board-panel">
      <div class="card-head">
        <div><h3>新增绩效板块</h3><p>先上传同事已经做好的 Skill.md；底表可进入板块后一次上传多张。</p></div>
        <button class="button" id="cancel-new-board" type="button">取消</button>
      </div>
      <form id="board-rule-upload-form" class="simple-upload-form">
        <label>板块名称<input name="business_scope_name" maxlength="256" placeholder="例如：原告案件" required /></label>
        <label>Skill.md<input name="file" type="file" accept=".md,.zip" required /></label>
        <button class="button primary" type="submit">上传并进入</button>
      </form>
    </section>`;
}

function bindPerformanceBoardHome() {
  document.querySelectorAll(".board-card").forEach(card => card.addEventListener("click", () => {
    state.selectedRule = card.dataset.rule || "";
    state.selectedDimension = card.dataset.dimension || "";
    const params = new URLSearchParams({ page: "performance" });
    if (state.selectedRule) params.set("rule", state.selectedRule);
    else params.set("dimension", state.selectedDimension);
    history.replaceState({}, "", `?${params.toString()}`);
    loadPerformance();
  }));
  $("#new-board-toggle")?.addEventListener("click", () => {
    $("#new-board-panel")?.classList.remove("hidden");
    $("#new-board-panel")?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("#cancel-new-board")?.addEventListener("click", () => {
    $("#new-board-panel")?.classList.add("hidden");
  });
  $("#board-rule-upload-form")?.addEventListener("submit", event => uploadBoardRule(event));
}

async function uploadBoardRule(event) {
  event.preventDefault();
  const result = await mutate(
    event.submitter,
    () => api("rules/upload", { method: "POST", body: new FormData(event.currentTarget) }),
    "Skill.md 已上传，正在进入板块",
    false,
  );
  state.selectedRule = result.rule_ref || "";
  state.selectedDimension = new FormData(event.currentTarget).get("business_scope_name") || "";
  const params = new URLSearchParams({ page: "performance" });
  if (state.selectedRule) params.set("rule", state.selectedRule);
  history.replaceState({}, "", `?${params.toString()}`);
  await loadPerformance();
}

async function loadPerformanceBoardWorkspace(ruleRef) {
  let rule = null;
  let amendments = [];
  let sourceFiles = [];
  if (ruleRef) {
    const periodQuery = state.selectedPeriod
      ? `?period_ref=${encodeURIComponent(state.selectedPeriod)}`
      : "";
    [rule, amendments, sourceFiles] = await Promise.all([
      api(`rules/${encodeURIComponent(ruleRef)}`),
      api(`rules/${encodeURIComponent(ruleRef)}/amendments`),
      api(`rules/${encodeURIComponent(ruleRef)}/source-files${periodQuery}`),
    ]);
    state.selectedDimension = rule.business_scope_name || rule.skill_name;
  }
  const currentPeriod = state.periods.find(
    item => item.period_ref === state.selectedPeriod,
  ) || state.periods[0];
  const readiness = performanceSourceReadiness(rule, sourceFiles);
  const reportControls = performanceReportControls(currentPeriod);
  const metricKey = rule && currentPeriod
    ? reportPreviewKey(
      rule.rule_ref,
      currentPeriod.period_ref,
      reportControls.view,
      reportControls.anchorDate,
      reportControls.scopeKey,
    )
    : "";
  if (!state.workspaceTab && rule) {
    state.workspaceTab = readiness.canPreview
      ? "metric-preview-section"
      : "source-files-section";
  }
  content.innerHTML = renderPerformanceBoardWorkspace(rule, amendments, sourceFiles);
  bindPerformanceBoardWorkspace(rule, amendments, sourceFiles);
  if (
    rule
    && currentPeriod
    && rule.rule_spec_hash
    && readiness.canPreview
    && !state.metricPreviews.has(metricKey)
  ) {
    const target = $("#metric-preview-result");
    try {
      const preview = await api(
        `rules/${encodeURIComponent(rule.rule_ref)}/report-preview`,
        {
          method: "POST",
          body: JSON.stringify({
            period_ref: currentPeriod.period_ref,
            view: reportControls.view,
            anchor_date: reportControls.anchorDate,
            scope_key: reportControls.scopeKey,
          }),
        },
      );
      state.metricPreviews.set(metricKey, preview);
      showPerformanceReportPreview(preview, reportControls, { target });
    } catch (error) {
      if (target) {
        target.innerHTML = `<div class="report-empty-state report-error-state">
          <strong>这次预览没有生成</strong>
          <p>${escapeHtml(error.message)}</p>
          <button class="button" id="retry-auto-preview" type="button">重新尝试</button>
        </div>`;
        $("#performance-report-status").textContent = "生成失败，请检查提示后重试。";
        $("#retry-auto-preview")?.addEventListener("click", () => {
          $("#refresh-performance-report")?.click();
        });
      }
    }
  }
}

function performanceSourceReadiness(rule, sourceFiles) {
  const requiredTables = (rule?.source_tables || []).filter(table => table.required !== false);
  const latestByTable = new Map();
  for (const source of sourceFiles || []) {
    for (const validation of source.validations || []) {
      const existing = latestByTable.get(validation.table_key);
      if (!existing || Number(validation.version || 0) > Number(existing.version || 0)) {
        latestByTable.set(validation.table_key, validation);
      }
    }
  }
  const ready = requiredTables.filter(table =>
    ["ready", "published"].includes(latestByTable.get(table.key)?.status));
  const errors = requiredTables.filter(table =>
    latestByTable.get(table.key)?.status === "validation_failed");
  const missing = requiredTables.filter(table => !latestByTable.has(table.key));
  return {
    requiredTables,
    latestByTable,
    ready,
    errors,
    missing,
    canPreview: Boolean(requiredTables.length)
      && ready.length === requiredTables.length,
  };
}

function previewKey(ruleRef, periodRef, view = "", anchorDate = "", scopeKey = "") {
  return [ruleRef, periodRef, view, anchorDate, scopeKey]
    .map(value => String(value || ""))
    .join(":");
}

function reportPreviewKey(ruleRef, periodRef, view, anchorDate, scopeKey) {
  return previewKey(ruleRef, periodRef, view, anchorDate, scopeKey);
}

function performanceReportPayload(result) {
  if (!result || typeof result !== "object") return {};
  if (result.report && typeof result.report === "object") return result.report;
  if (result.data && typeof result.data === "object") return result.data;
  return result;
}

function performanceReportControls(currentPeriod) {
  const startsOn = String(currentPeriod?.starts_on || "");
  const endsOn = String(currentPeriod?.ends_on || "");
  let anchorDate = String(state.performanceReportAnchorDate || endsOn || "");
  if (startsOn && anchorDate && anchorDate < startsOn) anchorDate = startsOn;
  if (endsOn && anchorDate && anchorDate > endsOn) anchorDate = endsOn;
  state.performanceReportAnchorDate = anchorDate;
  return {
    periodRef: String(currentPeriod?.period_ref || ""),
    view: state.performanceReportView === "week" ? "week" : "month",
    anchorDate,
    scopeKey: String(state.performanceReportScopeKey || "__total__"),
  };
}

function performanceReportViewHref(nextView, controls) {
  const params = new URLSearchParams(location.search);
  params.set("page", "performance");
  if (state.selectedRule) params.set("rule", state.selectedRule);
  params.set("view", nextView === "week" ? "week" : "month");
  if (controls.anchorDate) params.set("anchor_date", controls.anchorDate);
  else params.delete("anchor_date");
  if (controls.scopeKey) params.set("scope", controls.scopeKey);
  else params.delete("scope");
  return `?${params.toString()}`;
}

function selectedPerformanceScope(result, requestedScopeKey = "") {
  const report = performanceReportPayload(result);
  const scopes = Array.isArray(report.scopes) ? report.scopes : [];
  return scopes.find(item => String(item.scope_key || "") === String(requestedScopeKey || ""))
    || scopes.find(item => String(item.scope_name || "") === "整体")
    || scopes[0]
    || null;
}

function reportScopeLabel(scope) {
  return String(scope?.scope_name || "") === "整体"
    ? "部门整体"
    : String(scope?.scope_name || "未命名团队");
}

function reportRateText(rate) {
  if (rate && typeof rate === "object") {
    return String(rate.display || rate.display_text || "暂不可比");
  }
  const value = Number(rate);
  if (!Number.isFinite(value)) return "暂不可比";
  if (value < 0) return `下降${Math.abs(value).toFixed(2)}%`;
  if (value > 0) return `上升${value.toFixed(2)}%`;
  return "持平0.00%";
}

function reportTargetNote(target) {
  if (!target || typeof target !== "object") return "";
  const label = target.status_label || target.label || "";
  if (!label) return "";
  return `<span class="report-target-note ${statusClass(target.status)}">${escapeHtml(label)}</span>`;
}

function renderPerformanceScopeOptions(result, selectedKey) {
  const scopes = performanceReportPayload(result).scopes || [];
  if (!scopes.length) {
    return `<option value="__total__">部门整体</option>`;
  }
  return scopes.map(scope => {
    const key = String(scope.scope_key || "");
    return `<option value="${escapeHtml(key)}" ${key === String(selectedKey || "") ? "selected" : ""}>${escapeHtml(reportScopeLabel(scope))}</option>`;
  }).join("");
}

function renderPerformanceReportControls(rule, currentPeriod, savedPreview, readiness) {
  const controls = performanceReportControls(currentPeriod);
  const canPreview = Boolean(
    rule?.rule_spec_hash
    && currentPeriod
    && readiness.canPreview
    && state.shell.identity.capabilities.view,
  );
  const hasReport = Boolean(
    selectedPerformanceScope(savedPreview, controls.scopeKey),
  );
  return `<div class="performance-report-controls">
    <div class="report-control-block report-view-control">
      <span class="report-control-label">查看维度</span>
      <div class="report-dimension-switch" role="group" aria-label="月维度或周维度">
        <a href="${escapeHtml(performanceReportViewHref("month", controls))}" data-report-view="month" class="${controls.view === "month" ? "active" : ""}" aria-current="${controls.view === "month" ? "page" : "false"}">月维度</a>
        <a href="${escapeHtml(performanceReportViewHref("week", controls))}" data-report-view="week" class="${controls.view === "week" ? "active" : ""}" aria-current="${controls.view === "week" ? "page" : "false"}">周维度</a>
      </div>
    </div>
    ${state.periods.length ? `<label class="report-control-block">考核周期
      <select id="rule-validation-period">
        ${state.periods.map(item => `<option value="${escapeHtml(item.period_ref)}" ${item.period_ref === currentPeriod?.period_ref ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
      </select>
    </label>` : ""}
    <label class="report-control-block">截止日期
      <input id="performance-report-anchor" type="date"
        value="${escapeHtml(controls.anchorDate)}"
        ${currentPeriod?.starts_on ? `min="${escapeHtml(currentPeriod.starts_on)}"` : ""}
        ${currentPeriod?.ends_on ? `max="${escapeHtml(currentPeriod.ends_on)}"` : ""}
        ${canPreview ? "" : "disabled"} />
    </label>
    <label class="report-control-block">查看范围
      <select id="performance-report-scope" ${hasReport ? "" : "disabled"}>
        ${renderPerformanceScopeOptions(savedPreview, controls.scopeKey)}
      </select>
    </label>
    <button class="button primary report-refresh" id="refresh-performance-report" type="button" ${canPreview ? "" : "disabled"}>刷新预览</button>
    <div class="report-export-actions" aria-label="导出报告">
      <button class="button report-export" data-format="docx" type="button" ${hasReport ? "" : "disabled"}>导出 Word 报告</button>
      <button class="button report-export" data-format="xlsx" type="button" ${hasReport ? "" : "disabled"}>导出 Excel 明细</button>
    </div>
  </div>
  <div class="report-control-help">
    <span>${controls.view === "week"
      ? "周维度按周一至截止日统计；选择周五可生成完整周报。"
      : "月维度从当月1日统计至截止日，并与上月末比较。"}</span>
    <span id="performance-report-status">${hasReport ? "预览已生成，可切换团队或导出。" : "选择范围后刷新预览。"}</span>
  </div>`;
}

function renderPerformanceBoardWorkspace(rule, amendments, sourceFiles) {
  const caps = state.shell.identity.capabilities;
  const dimension = rule?.business_scope_name || state.selectedDimension || "新绩效板块";
  const activeSourceFiles = (sourceFiles || []).filter(item => item.status === "active");
  const sourceEditable = Boolean(
    rule && caps.performance_upload
    && ["uploaded", "understanding_draft", "awaiting_confirmation", "active"].includes(rule.status),
  );
  const ruleEditable = Boolean(
    rule && caps.performance_upload
    && ["understanding_draft", "awaiting_confirmation"].includes(rule.status),
  );
  const canStartRevision = Boolean(
    rule && caps.performance_upload && rule.status === "active",
  );
  const guidanceEditable = ruleEditable || canStartRevision;
  const metrics = rule?.metric_catalog || [];
  const unresolved = rule?.unresolved || [];
  const resolutionMap = new Map((rule?.issue_resolutions || []).map(item => [item.issue, item.response]));
  const currentPeriod = state.periods.find(item => item.period_ref === state.selectedPeriod) || state.periods[0];
  const readiness = performanceSourceReadiness(rule, activeSourceFiles);
  const reportControls = performanceReportControls(currentPeriod);
  const savedPreview = rule && currentPeriod
    ? state.metricPreviews.get(reportPreviewKey(
      rule.rule_ref,
      currentPeriod.period_ref,
      reportControls.view,
      reportControls.anchorDate,
      reportControls.scopeKey,
    ))
    : null;
  return `
    <div class="workspace-breadcrumb">
      <button class="text-button" id="back-to-boards" type="button">绩效板块</button>
      <span>/</span><strong>${escapeHtml(dimension)}</strong>
    </div>
    <section class="workspace-intro">
      <div>
        <p class="eyebrow">板块维护台</p>
        <h2>${escapeHtml(dimension)}</h2>
        <p>${rule ? "Skill、底表和指标都集中在这一页维护。" : "先上传这个板块的 Skill.md，系统理解后会展开底表和指标。"}</p>
      </div>
      <div class="toolbar">
        ${rule ? `<span class="badge ${statusClass(rule.status)}">${escapeHtml(rule.status_label)}</span>` : `<span class="badge info">尚未配置</span>`}
        ${rule ? `<button class="button" id="open-advanced-rule" type="button">完整规则设置</button>` : ""}
      </div>
    </section>
    <nav class="workspace-steps" aria-label="板块维护内容">
      <button type="button" data-workspace-tab="skill-section"><span>1</span>Skill.md</button>
      <button type="button" data-workspace-tab="rule-detail-section"><span>2</span>规则详细内容</button>
      <button type="button" data-workspace-tab="source-files-section"><span>3</span>底表与数据</button>
      <button type="button" data-workspace-tab="metric-preview-section"><span>4</span>指标完成情况</button>
    </nav>

    <section class="workspace-section" id="skill-section" data-workspace-panel>
      <div class="workspace-section-head">
        <div><span class="section-number">1</span><h3>Skill.md</h3><p>这是系统理解本板块口径的原始依据。</p></div>
        ${rule ? `<span class="badge ${rule.skill_markdown ? "good" : "warn"}">${rule.skill_markdown ? "已上传" : "内容待读取"}</span>` : ""}
      </div>
      ${rule ? `
        <div class="skill-summary">
          <div><span>文件</span><strong>${escapeHtml(rule.skill_file_name || "SKILL.md")}</strong></div>
          <div><span>版本</span><strong>${escapeHtml(rule.rule_version || "待确认")}</strong></div>
          <div><span>上传时间</span><strong>${formatTime(rule.created_at)}</strong></div>
        </div>
        <div class="toolbar">
          ${rule.status === "uploaded" && caps.performance_upload ? `<button class="button primary" id="interpret-board-rule" type="button">让系统读取并理解</button>` : ""}
          <details class="skill-source">
            <summary>查看 Skill.md 原文</summary>
            <pre>${escapeHtml(rule.skill_markdown || "Skill.md 内容尚未读取")}</pre>
          </details>
        </div>
        ${caps.performance_upload ? `<form id="replace-board-rule-form" class="inline-upload-form">
          <input name="business_scope_name" type="hidden" value="${escapeHtml(dimension)}" />
          <label>上传新版 Skill.md<input name="file" type="file" accept=".md,.zip" required /></label>
          <button class="button" type="submit">上传新版本</button>
        </form>` : ""}` : `
        <div class="plain-empty">
          <strong>还没有 Skill.md</strong>
          <p>上传同事已有的 Skill 或技能包即可。没有明确规则时，系统不会编造绩效公式。</p>
          ${caps.performance_upload ? `<form id="empty-board-rule-upload-form" class="simple-upload-form">
            <input name="business_scope_name" type="hidden" value="${escapeHtml(dimension)}" />
            <label>选择 Skill.md<input name="file" type="file" accept=".md,.zip" required /></label>
            <button class="button primary" type="submit">上传并读取</button>
          </form>` : ""}
        </div>`}
    </section>

    <section class="workspace-section" id="rule-detail-section" data-workspace-panel>
      <div class="workspace-section-head">
        <div><span class="section-number">2</span><h3>规则详细内容</h3><p>系统的理解、待确认事项和沟通修改都在这里。</p></div>
        ${rule ? `<span class="badge ${unresolved.length ? "warn" : "good"}">${unresolved.length ? `${unresolved.length} 项待确认` : "暂无待确认事项"}</span>` : ""}
      </div>
      ${rule ? `
        <div class="rule-summary-panel">
          <span>系统理解摘要</span>
          <p>${escapeHtml(rule.rule_summary || "尚未形成摘要，请先让系统读取并理解 Skill.md。")}</p>
        </div>
        ${(rule.assignment_chains || []).length ? `<div class="assignment-summary-strip">
          <div><strong>本板块团队归属</strong>
            <p>${escapeHtml((rule.assignment_chains[0].path || []).join(" → "))}</p>
          </div>
          <span class="badge ${rule.assignment_chains.some(chain => chain.direct_field_fallback) ? "warn" : "good"}">${rule.assignment_chains.some(chain => chain.direct_field_fallback) ? "存在底表字段备用口径" : "不使用底表团队列"}</span>
        </div>` : ""}
        ${unresolved.length ? `<div class="confirmation-list">
          <div class="confirmation-heading"><strong>请确认这些业务口径</strong><span>逐项回复后，系统只生成修改建议，不会自动生效。</span></div>
          ${unresolved.map((issue, index) => `<label class="confirmation-item">
            <span>${index + 1}. ${escapeHtml(issue)}</span>
            <textarea class="issue-resolution" data-index="${index}" rows="2" maxlength="4000"
              placeholder="直接用业务语言填写你的确认结果" ${ruleEditable ? "" : "disabled"}>${escapeHtml(resolutionMap.get(issue) || "")}</textarea>
          </label>`).join("")}
          <button class="button primary" id="submit-issue-resolutions" type="button" ${ruleEditable ? "" : "disabled"}>保存答复并生成修改建议</button>
        </div>` : ""}
        <form id="rule-guidance-form" class="guidance-box">
          <div><strong>需要修改规则？直接告诉系统</strong><p>${canStartRevision
            ? "提交后会建立一份新的修改草稿；当前正在使用的规则不会被改动。"
            : "例如：“团队归属按法务负责的分公司计算，不使用底表里的团队列。”"}</p></div>
          <textarea name="instruction" rows="3" maxlength="8000" placeholder="请描述需要增加、删除或调整的业务口径" ${guidanceEditable ? "" : "disabled"}></textarea>
          <button class="button" type="submit" ${guidanceEditable ? "" : "disabled"}>${canStartRevision ? "生成新版本修改稿" : "沟通修改"}</button>
        </form>
        ${(amendments || []).length ? `<details class="amendment-list"><summary>查看 ${amendments.length} 次修改记录</summary>
          <div class="stack compact-stack">${amendments.map(renderRuleAmendment).join("")}</div>
        </details>` : ""}
        ${caps.publish && rule.status === "awaiting_confirmation" ? `<div class="final-rule-confirm">
          ${rule.requires_assignment_confirmation ? `<label class="confirmation-check">
            <input id="assignment-confirmed" type="checkbox" />
            我已核对团队与分公司的归属关系，没有直接采用底表中的团队列
          </label>` : ""}
          <button class="button primary" id="workspace-confirm-rule" data-hash="${escapeHtml(rule.understanding_hash)}">确认这版规则</button>
        </div>` : ""}` : `<div class="plain-empty"><strong>等待 Skill.md</strong><p>上传并读取 Skill 后，这里会展示详细规则和所有待确认问题。</p></div>`}
    </section>

    <section class="workspace-section" id="source-files-section" data-workspace-panel>
      <div class="workspace-section-head">
        <div><span class="section-number">3</span><h3>底表与数据</h3><p>直接上传同事正在使用的原表；识别、问题和数据预览都在这里完成。</p></div>
        ${rule ? `<span class="badge ${readiness.canPreview ? "good" : "info"}">${readiness.canPreview ? "底表已准备好" : `${activeSourceFiles.length} 张底表`}</span>` : ""}
      </div>
      ${rule ? `
        <form id="rule-source-file-form" class="source-dropzone">
          <label><strong>上传现有底表</strong><span>不需要套模板。支持 xlsx、csv，可一次选择多张。</span>
            <input name="file" type="file" accept=".xlsx,.csv" multiple required ${sourceEditable ? "" : "disabled"} />
          </label>
          <button class="button primary" type="submit" ${sourceEditable ? "" : "disabled"}>上传并检查数据</button>
        </form>
        ${!state.periods.length ? `<div class="inline-notice">校验底表前需要一个考核周期。<button class="text-button" id="new-period" type="button">现在新建</button></div>` : ""}
        ${currentPeriod ? `<div class="source-period-note"><span>当前查看周期</span><strong>${escapeHtml(currentPeriod.label)}</strong><small>更换周期请到“指标完成情况”选择</small></div>` : ""}
        <div id="period-form-slot"></div>
        ${activeSourceFiles.length ? `<div class="source-file-list">
          ${activeSourceFiles.map(item => renderPerformanceSourceFile(item, sourceEditable, rule, currentPeriod)).join("")}
        </div>` : `<div class="plain-empty"><strong>尚未上传底表</strong><p>同一个 Skill 需要多张底表时，可以一次全部选中上传，也可以以后逐张补充或替换。</p></div>`}` :
        `<div class="plain-empty"><strong>先上传 Skill.md</strong><p>系统读取规则后，才知道本板块需要哪些底表。</p></div>`}
    </section>

    <section class="workspace-section" id="metric-preview-section" data-workspace-panel>
      <div class="workspace-section-head">
        <div><span class="section-number">4</span><h3>指标完成情况</h3><p>按 Skill 中已经确认的公式计算，不使用大模型算分，也不会自动发布。</p></div>
        ${rule ? `<span class="badge ${metrics.length ? "good" : "warn"}">${metrics.length ? `${metrics.length} 项指标` : "指标待确认"}</span>` : ""}
      </div>
      ${rule ? `
        <div class="metric-dashboard-bar">
          <div>
            <span>当前绩效结果</span>
            <strong>${escapeHtml(currentPeriod?.label || "请选择周期")}</strong>
            <small>${readiness.canPreview
              ? "已按当前 Skill 和通过校验的底表计算"
              : "底表准备好后，这里会直接显示结果"}</small>
          </div>
          <div class="toolbar">
            <button class="button quick-replace-source" type="button" ${sourceEditable ? "" : "disabled"}>替换或补充底表</button>
            <button class="button switch-workspace-tab" data-target-tab="skill-section" type="button">查看 Skill.md</button>
          </div>
        </div>
        <div class="metric-readiness ${readiness.canPreview ? "is-ready" : "needs-data"}">
          <div>
            <span>底表准备情况</span>
            <strong>${readiness.canPreview
              ? `${readiness.ready.length}/${readiness.requiredTables.length} 张必需底表已通过`
              : `${readiness.ready.length}/${readiness.requiredTables.length || 0} 张必需底表已通过`}</strong>
            <p>${readiness.canPreview
              ? "现在可以直接计算并查看这个板块的指标。"
              : readiness.errors.length
                ? `${readiness.errors.map(table => table.name || table.key).join("、")}仍有问题，请先回到底表查看。`
                : `还缺少：${readiness.missing.map(table => table.name || table.key).join("、") || "通过校验的底表"}`}</p>
          </div>
          ${readiness.canPreview ? "" : `<button class="button switch-workspace-tab" data-target-tab="source-files-section" type="button">去检查底表</button>`}
        </div>
        ${renderPerformanceReportControls(rule, currentPeriod, savedPreview, readiness)}
        <div id="metric-preview-result">${savedPreview
          ? renderMetricPreviewResult(savedPreview, reportControls.scopeKey)
          : readiness.canPreview
            ? `<div class="metric-preview-loading"><span></span><strong>正在按 Skill 计算指标并生成周报 / 月报预览</strong><small>只读取已校验底表，不会发布或修改业务数据</small></div>`
            : ""}</div>
        ${metrics.length ? `<details class="metric-definition-list"><summary>查看 ${metrics.length} 项指标口径</summary>
          <div class="metric-catalog-grid">${metrics.map(item => `<article class="metric-catalog-card">
            <span>绩效指标</span>
            <strong>${escapeHtml(businessMetricName(ruleValue(item, ["metric_name", "name", "label"])))}</strong>
            <p>${escapeHtml(ruleValue(item, ["description", "purpose", "definition"], "等待底表验证后展示完成情况"))}</p>
            <small>${escapeHtml(ruleValue(item, ["unit"], "按 Skill 原文"))}</small>
          </article>`).join("")}</div>
        </details>` :
          `<div class="plain-empty"><strong>还没有可展示的指标</strong><p>系统不会根据少量文字猜测公式；请先完成 Skill 理解和待确认事项。</p></div>`}` :
        `<div class="plain-empty"><strong>等待规则与底表</strong><p>准备完成后，这里会按团队或板块展示实际指标完成情况。</p></div>`}
    </section>`;
}

function friendlyCellValue(value) {
  if (value === undefined || value === null || value === "") return "—";
  if (Array.isArray(value)) return value.map(item => ruleText(item, "")).filter(Boolean).join("、") || "—";
  if (typeof value === "object") return ruleText(value);
  return String(value);
}

function renderSourceDataPreview(validation) {
  const rows = validation?.preview_rows || [];
  const columns = [];
  for (const row of rows) {
    for (const key of Object.keys(row.data || {})) {
      if (!columns.includes(key)) columns.push(key);
      if (columns.length >= 6) break;
    }
    if (columns.length >= 6) break;
  }
  if (!rows.length) {
    return `<div class="source-preview-empty">这次没有可展示的数据行，请查看具体问题后重新校验。</div>`;
  }
  return `<div class="source-data-preview">
    <div class="source-data-preview-head">
      <strong>校验后的数据</strong>
      <span>先展示 ${rows.length} 行，共 ${validation.total_preview_rows || validation.row_count || 0} 行</span>
    </div>
    <div class="source-data-table-wrap"><table>
      <thead><tr><th>原表行号</th>${columns.map(column => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(row => `<tr class="${row.status === "error" ? "has-error" : ""}">
        <td>${row.source_row_number || "—"}</td>
        ${columns.map(column => `<td>${escapeHtml(friendlyCellValue((row.data || {})[column]))}</td>`).join("")}
      </tr>`).join("")}</tbody>
    </table></div>
  </div>`;
}

function renderPerformanceSourceFile(item, sourceEditable, rule, currentPeriod) {
  const profile = item.inspection || {};
  const tables = rule.source_tables || [];
  const mappings = profile.field_mappings || {};
  const mappedTables = Object.values(mappings).filter(value => value && value.mapping);
  const validations = [...(item.validations || [])].sort((left, right) =>
    Number(right.version || 0) - Number(left.version || 0));
  const validation = validations[0] || null;
  const selectedTableKey = validation?.table_key || tables[0]?.key || "";
  const selectedTable = tables.find(table => table.key === selectedTableKey) || tables[0] || {};
  const selectedMapping = mappings[selectedTableKey] || {};
  const mappedFieldCount = Object.keys(selectedMapping.mapping || {}).length;
  const validationReady = ["ready", "published"].includes(validation?.status);
  const validationFailed = validation?.status === "validation_failed";
  const validationWarningCount = Number(validation?.warning_count || 0);
  const canValidate = sourceEditable && rule.rule_spec_hash && currentPeriod && tables.length;
  const unavailableReason = !rule.rule_spec_hash
    ? "请先完成 Skill 理解，系统才能按规则检查数据。"
    : !currentPeriod
      ? "请先建立考核周期，再检查底表。"
      : !sourceEditable
        ? "当前账号只能查看，不能上传或重新校验。"
        : "当前 Skill 还没有声明需要的底表。";
  const errorMessages = (validation?.preview_rows || [])
    .flatMap(row => row.errors || [])
    .map(error => error.message)
    .filter(Boolean)
    .slice(0, 3);
  return `<article class="source-file-card ${validationReady ? "is-ready" : validationFailed ? "has-errors" : "needs-validation"}"
    data-source-file="${escapeHtml(item.source_file_ref)}">
    <div class="source-file-main">
      <div>
        <div class="toolbar">
          <strong>${escapeHtml(item.business_label || item.file_name)}</strong>
          <span class="badge ${validationReady ? "good" : validationFailed ? "bad" : mappedTables.length ? "info" : "warn"}">
            ${validationReady ? "校验通过" : validationFailed ? "有数据问题" : mappedTables.length ? "字段已识别，待校验" : "已读取，待校验"}
          </span>
        </div>
        <p>${escapeHtml(item.file_name)} · ${profile.total_data_rows || 0} 行 · ${formatTime(item.uploaded_at)}</p>
      </div>
      ${sourceEditable && item.can_abandon ? `<button class="text-button danger-text abandon-rule-source-file" type="button"
        data-source-file="${escapeHtml(item.source_file_ref)}">移出</button>` : ""}
    </div>
    <div class="source-file-facts">
      <div><span>原表数据</span><strong>${profile.total_data_rows || 0} 行</strong></div>
      <div><span>字段识别</span><strong>${mappedFieldCount ? `${mappedFieldCount} 个已对应` : "等待校验"}</strong></div>
      <div><span>当前结果</span><strong>${validationReady
        ? validationWarningCount
          ? `可以计算 · ${validationWarningCount} 行提醒`
          : "可以计算指标"
        : validationFailed ? `${validation.error_count || 0} 行有问题` : "还未校验"}</strong></div>
    </div>
    ${(profile.warnings || []).length ? `<div class="source-warning-list">${profile.warnings.map(message => `<div class="inline-notice">${escapeHtml(message)}</div>`).join("")}</div>` : ""}
    ${validation ? `<div class="source-validation-summary ${validationReady ? "success" : "error"}">
      <div class="source-validation-summary-head">
        <div><strong>${validationReady
          ? validationWarningCount
            ? `数据可以用于指标，另有 ${validationWarningCount} 行提醒`
            : "数据已经校验通过"
          : "这张底表还不能用于指标"}</strong>
          <p>${validationReady
            ? `${escapeHtml(validation.table_name)} · ${validation.row_count || 0} 行 · ${formatTime(validation.validated_at)}${validationWarningCount ? " · 提醒行不会影响本周期指标" : ""}`
            : `${escapeHtml(validation.table_name)}发现 ${validation.error_count || 0} 行问题，请修正后重新上传或校验。`}</p></div>
        <span class="badge ${validationReady ? "good" : "bad"}">${escapeHtml(validation.status_label)}</span>
      </div>
      ${errorMessages.length ? `<div class="source-error-summary">${errorMessages.map(message => `<p>${escapeHtml(message)}</p>`).join("")}</div>` : ""}
      ${renderSourceDataPreview(validation)}
      <div class="source-result-actions">
        ${validationWarningCount ? `<button class="button primary view-source-warnings" type="button" data-batch="${escapeHtml(validation.batch_no)}">查看 ${validationWarningCount} 行提醒</button>` : ""}
        ${validationFailed ? `<button class="button primary view-source-errors" type="button" data-batch="${escapeHtml(validation.batch_no)}">查看 ${validation.error_count || 0} 行问题</button>` : ""}
        <button class="button view-source-data" type="button" data-batch="${escapeHtml(validation.batch_no)}">查看全部数据</button>
        ${validationFailed ? `<button class="button download-source-errors" type="button" data-batch="${escapeHtml(validation.batch_no)}">下载问题数据</button>` : ""}
      </div>
    </div>` : `<div class="source-next-action">
      <div><strong>下一步：检查整张底表</strong><p>系统会按 Skill 对应字段，并在这里直接展示校验后的真实数据。</p></div>
    </div>`}
    <details class="source-structure">
      <summary>查看识别到的工作表和字段</summary>
      ${(profile.sheets || []).map(sheet => `<div class="sheet-summary">
        <strong>${escapeHtml(sheet.sheet_name)}</strong><span>${sheet.row_count} 行 · ${sheet.column_count} 列</span>
        <div class="column-chip-list">${(sheet.columns || []).map(column => `<span>${escapeHtml(column.header)}</span>`).join("")}</div>
      </div>`).join("")}
      ${mappedTables.length ? `<div class="mapping-summary">${mappedTables.map(record => `<div>
        <strong>${escapeHtml(record.table_name || "规则源表")}</strong>
        <span>${Object.keys(record.mapping || {}).length} 个字段已确认，以后同类底表会自动沿用</span>
      </div>`).join("")}</div>` : ""}
    </details>
    ${canValidate ? `<div class="source-validation-row">
      ${tables.length === 1
        ? `<span class="source-table-purpose">用于：<strong>${escapeHtml(selectedTable.name || selectedTable.key || "本板块底表")}</strong></span>
          <input class="source-validation-table" type="hidden" value="${escapeHtml(selectedTableKey)}" />`
        : `<label>这张底表用于<select class="source-validation-table">
          ${tables.map(table => `<option value="${escapeHtml(table.key || "")}" ${table.key === selectedTableKey ? "selected" : ""}>${escapeHtml(table.name || table.key || "源表")}</option>`).join("")}
        </select></label>`}
      <input class="source-validation-period" type="hidden" value="${escapeHtml(currentPeriod.period_ref)}" />
      <button class="button ${validationReady ? "" : "primary"} validate-existing-rule-source-file" type="button">
        ${validationReady ? "重新校验" : validationFailed ? "修正后重新校验" : "校验并预览数据"}
      </button>
      <span>不会修改原文件，也不会自动发布。</span>
    </div>` : `<div class="inline-notice">${escapeHtml(unavailableReason)}</div>`}
    <div class="source-mapping-slot"></div>
  </article>`;
}

function renderColumnMappingConfirmation(result) {
  const mapping = result.mapping || {};
  return `<form class="column-mapping-form">
    <div class="mapping-form-head">
      <div><strong>请确认原表字段</strong><p>${escapeHtml(result.message || "原表不需要修改，只需确认字段对应关系。")}</p></div>
      <span class="badge warn">${escapeHtml(result.sheet_name || "数据工作表")}</span>
    </div>
    <div class="mapping-form-grid">
      ${(mapping.items || []).map(item => `<label>
        <span>${escapeHtml(item.field_name)}</span>
        <select data-field-key="${escapeHtml(item.field_key)}" required>
          <option value="">请选择原表中的字段</option>
          ${(result.workbook_headers || []).map(header => `<option value="${escapeHtml(header)}" ${header === item.source_header ? "selected" : ""}>${escapeHtml(header)}</option>`).join("")}
        </select>
        <small>${escapeHtml(item.method_label || "需要确认")}</small>
      </label>`).join("")}
    </div>
    <button class="button primary confirm-column-mapping" type="submit">确认字段并校验</button>
  </form>`;
}

function renderAssignmentErrorSummary(errors) {
  if (!errors.length) return "";
  const groups = new Map();
  for (const error of errors) {
    const sourceValue = String(error.source_value || "未识别值");
    const message = String(error.message || "无法通过已确认的映射关系确定团队");
    const key = `${sourceValue}\n${message}`;
    const current = groups.get(key) || { sourceValue, message, count: 0 };
    current.count += 1;
    groups.set(key, current);
  }
  return `<section class="report-assignment-warning">
    <div>
      <span class="report-warning-mark">!</span>
      <div><strong>有 ${errors.length} 条数据无法归属</strong>
        <p>这些数据没有被算进任何团队；系统没有根据名称猜测。你可以查看归属值后再决定是否补充规则。</p>
      </div>
    </div>
    <div class="report-warning-list">
      ${[...groups.values()].slice(0, 8).map(item => `<div>
        <strong>${escapeHtml(item.sourceValue)}</strong>
        <span>${escapeHtml(item.message)}${item.count > 1 ? ` · ${item.count} 条` : ""}</span>
      </div>`).join("")}
    </div>
    ${groups.size > 8 ? `<p class="report-more-note">另有 ${groups.size - 8} 种无法归属情况，可在底表校验结果中继续查看。</p>` : ""}
    <button class="button metric-rule-guidance" type="button">去补充团队归属</button>
  </section>`;
}

function renderDataQualitySummary(errors) {
  if (!errors.length) return "";
  return `<section class="report-data-quality-warning">
    <strong>有 ${errors.length} 条数据存在完整性问题</strong>
    <p>已结案但没有结案日期的案件不会计入存量；请按原表行号补充后重新上传。</p>
    <div class="report-warning-list">${errors.slice(0, 8).map(item => `<div>
      <strong>原表第 ${Number(item.source_row_number || 0)} 行</strong>
      <span>${escapeHtml(item.case_name || "案件名称未识别")} · ${escapeHtml(item.message || "数据不完整")}</span>
    </div>`).join("")}</div>
  </section>`;
}

function renderLossMetricSummary(scope) {
  if (String(scope?.scope_type || "") !== "overall") return "";
  const loss = scope?.loss_metrics || {};
  const comprehensive = loss.comprehensive_loss_rate || {};
  const substantial = loss.substantial_loss_amount || {};
  if (!comprehensive.status && !substantial.status) return "";
  if (comprehensive.status === "not_applicable" && substantial.status === "not_applicable") {
    return `<section class="report-loss-summary report-loss-unavailable">
      <header><div><span>减损指标</span><strong>仅支持月维度</strong></div>
        <small>当前 Skill 未定义周维度减损口径，本周不计算；切换到月维度即可查看。</small></header>
    </section>`;
  }
  return `<section class="report-loss-summary">
    <header><div><span>减损指标</span><strong>部门整体口径</strong></div>
      <small>综合减损按当前统计期；实质减损按年初至截止日</small></header>
    <div>
      <article><span>综合减损率</span><strong>${escapeHtml(comprehensive.display || "暂不可计算")}</strong>
        <small>${Number(comprehensive.eligible_case_count || 0)} 件结案案件纳入计算</small></article>
      <article><span>实质减损金额</span><strong>${escapeHtml(substantial.display || "暂不可计算")}</strong>
        <small>本年度累计 · ${Number(substantial.eligible_case_count || 0)} 件</small></article>
    </div>
  </section>`;
}

function renderReportCaseList(items, emptyText) {
  if (!items.length) {
    return `<div class="report-detail-empty">${escapeHtml(emptyText)}</div>`;
  }
  return `<div class="report-case-list">${items.map(item => `<article>
    <div><strong>${escapeHtml(item.case_name || "案件名称未识别")}</strong>
      <span>${escapeHtml(item.branch_name || "分公司未填写")}</span>
    </div>
    <span class="report-case-owner">${escapeHtml(item.lawyer_name || "承办法务未填写")}</span>
  </article>`).join("")}</div>`;
}

function renderLegacyMetricPreviewResult(result) {
  const successful = (result.results || []).filter(item => !item.errors?.length);
  const overall = successful.find(item => item.person_name === "整体")
    || successful[0]
    || null;
  const teams = successful.filter(item => item !== overall);
  const headlineMetrics = (teams[0]?.value_items || overall?.value_items || []).slice(0, 4);
  return `<div class="metric-preview-panel">
    ${overall ? `<section class="overall-metric-section">
      <div class="result-section-head"><div><span>整体结果</span><strong>${escapeHtml(overall.person_name || "整体")}</strong></div>
        <small>${(overall.value_items || []).length} 项指标</small></div>
      <div class="overall-metric-grid">${(overall.value_items || []).map(metric => `<article>
        <span>${escapeHtml(metric.name)}</span>
        <strong>${escapeHtml(metricDisplayText(metric))}</strong>
        <small>${escapeHtml(metric.status_label || "按 Skill 计算")}</small>
      </article>`).join("")}</div>
    </section>` : ""}
    ${teams.length ? `<section class="team-metric-section">
      <div class="result-section-head"><div><span>团队对比</span><strong>${teams.length} 个团队</strong></div></div>
      <div class="team-metric-table"><table>
        <thead><tr><th>团队</th>${headlineMetrics.map(metric => `<th>${escapeHtml(metric.name)}</th>`).join("")}</tr></thead>
        <tbody>${teams.map(team => `<tr>
          <td><strong>${escapeHtml(team.person_name || "未命名团队")}</strong></td>
          ${headlineMetrics.map(definition => {
            const metric = (team.value_items || []).find(item => item.name === definition.name);
            return `<td>${metric ? escapeHtml(metricDisplayText(metric)) : "—"}</td>`;
          }).join("")}
        </tr>`).join("")}</tbody>
      </table></div>
      <div class="preview-result-list">${teams.map(team => `<details>
        <summary><strong>${escapeHtml(team.person_name || "未命名团队")}</strong><span>查看全部指标</span></summary>
        <div class="metric-result-grid">${(team.value_items || []).map(metric => `<div class="metric-result">
          <strong>${escapeHtml(metric.name)}</strong>
          <div class="metric-result-value">${escapeHtml(metricDisplayText(metric))}</div>
        </div>`).join("")}</div>
      </details>`).join("")}</div>
    </section>` : ""}
  </div>`;
}

function renderMetricPreviewResult(result, requestedScopeKey = state.performanceReportScopeKey) {
  const report = performanceReportPayload(result);
  if (!Array.isArray(report.scopes) && Array.isArray(result?.results)) {
    return renderLegacyMetricPreviewResult(result);
  }
  const period = report.period || {};
  const scope = selectedPerformanceScope(result, requestedScopeKey);
  const errors = Array.isArray(report.assignment_errors) ? report.assignment_errors : [];
  const dataQualityErrors = Array.isArray(report.data_quality_errors)
    ? report.data_quality_errors
    : [];
  if (!scope) {
    return `<div class="report-empty-state">
      <strong>暂时没有可展示的指标</strong>
      <p>${errors.length ? "当前数据都存在无法归属的问题，请先查看下方提示。" : "请确认底表已经校验通过，然后刷新预览。"}</p>
      ${renderAssignmentErrorSummary(errors)}
    </div>`;
  }
  const weekly = String(period.view || state.performanceReportView) === "week";
  const currentLabel = weekly ? "本周" : "本月";
  const comparisonLabel = String(period.comparison_label || (weekly ? "上周五" : "上月末"));
  const branches = Array.isArray(scope.branches) ? scope.branches : [];
  const newCases = Array.isArray(scope.period_new_cases) ? scope.period_new_cases : [];
  const closedCases = Array.isArray(scope.period_closed_cases) ? scope.period_closed_cases : [];
  const detailTab = ["branches", "new", "closed"].includes(state.performanceReportDetailTab)
    ? state.performanceReportDetailTab
    : "branches";
  return `<div class="metric-preview-panel performance-report-panel">
    <header class="performance-report-head">
      <div>
        <span class="report-period-kicker">${escapeHtml(period.label || (weekly ? "周维度" : "月维度"))}</span>
        <h4>${escapeHtml(reportScopeLabel(scope))} · 被告案件绩效</h4>
        <p>${escapeHtml(period.starts_on || "—")} 至 ${escapeHtml(period.cutoff_date || period.ends_on || "—")} · 归属按“分公司 → 法务对接人 → 团队”计算</p>
      </div>
      <span class="badge good">预览已生成</span>
    </header>
    ${renderAssignmentErrorSummary(errors)}
    ${renderDataQualitySummary(dataQualityErrors)}
    <section class="report-kpi-grid" aria-label="核心指标">
      <article class="report-kpi-card emphasis">
        <span>当前存量</span><strong>${Number(scope.stock_count || 0)}</strong><small>件</small>
      </article>
      <article class="report-kpi-card">
        <span>存量同比</span><strong>${escapeHtml(reportRateText(scope.stock_yoy))}</strong>
        ${reportTargetNote(scope.stock_target)}
      </article>
      <article class="report-kpi-card">
        <span>较${escapeHtml(comparisonLabel)}</span><strong>${escapeHtml(reportRateText(scope.stock_period_change))}</strong>
        <small>对比数 ${Number(scope.previous_stock_count || 0)} 件</small>
      </article>
      <article class="report-kpi-card">
        <span>年度新增同比</span><strong>${escapeHtml(reportRateText(scope.new_yoy))}</strong>
        <small>年度累计 ${Number(scope.year_to_date_new_count || 0)} 件</small>
        ${reportTargetNote(scope.new_target)}
      </article>
      <article class="report-kpi-card">
        <span>${currentLabel}新增</span><strong>${Number(scope.period_new_count || 0)}</strong><small>件</small>
      </article>
      <article class="report-kpi-card">
        <span>${currentLabel}结案</span><strong>${Number(scope.period_closed_count || 0)}</strong><small>件</small>
      </article>
    </section>
    ${renderLossMetricSummary(scope)}
    <section class="report-details">
      <div class="report-detail-tabs" role="tablist" aria-label="报告明细">
        <button type="button" data-report-detail="branches" class="${detailTab === "branches" ? "active" : ""}" aria-selected="${detailTab === "branches"}">分公司明细 <span>${branches.length}</span></button>
        <button type="button" data-report-detail="new" class="${detailTab === "new" ? "active" : ""}" aria-selected="${detailTab === "new"}">新增案件明细 <span>${newCases.length}</span></button>
        <button type="button" data-report-detail="closed" class="${detailTab === "closed" ? "active" : ""}" aria-selected="${detailTab === "closed"}">结案案件明细 <span>${closedCases.length}</span></button>
      </div>
      <div data-report-detail-panel="branches" ${detailTab === "branches" ? "" : "hidden"}>
        ${branches.length ? `<div class="report-branch-table"><table>
          <thead><tr>
            <th>分公司</th><th>承办法务</th><th>存量</th><th>存量同比</th>
            <th>较${escapeHtml(comparisonLabel)}</th><th>年度新增</th><th>新增同比</th>
            <th>${currentLabel}新增</th><th>${currentLabel}结案</th>
          </tr></thead>
          <tbody>${branches.map(branch => `<tr>
            <td><strong>${escapeHtml(branch.branch_name || "未填写")}</strong></td>
            <td>${escapeHtml(branch.lawyer_name || "未填写")}</td>
            <td>${Number(branch.stock_count || 0)}</td>
            <td>${escapeHtml(reportRateText(branch.stock_yoy))}</td>
            <td>${escapeHtml(reportRateText(branch.stock_period_change))}</td>
            <td>${Number(branch.year_to_date_new_count || 0)}</td>
            <td>${escapeHtml(reportRateText(branch.new_yoy))}</td>
            <td>${Number(branch.period_new_count || 0)}</td>
            <td>${Number(branch.period_closed_count || 0)}</td>
          </tr>`).join("")}</tbody>
        </table></div>` : `<div class="report-detail-empty">当前范围没有分公司明细。</div>`}
      </div>
      <div data-report-detail-panel="new" ${detailTab === "new" ? "" : "hidden"}>
        ${renderReportCaseList(newCases, `${currentLabel}没有新增案件。`)}
      </div>
      <div data-report-detail-panel="closed" ${detailTab === "closed" ? "" : "hidden"}>
        ${renderReportCaseList(closedCases, `${currentLabel}没有结案案件。`)}
      </div>
    </section>
  </div>`;
}

function bindMetricPreviewGuidance(result) {
  document.querySelectorAll(".metric-rule-guidance").forEach(button => {
    button.addEventListener("click", () => {
      activateWorkspaceTab("rule-detail-section", { scroll: true });
      const input = document.querySelector("#rule-guidance-form textarea");
      if (!input || input.disabled) return;
      const reportErrors = performanceReportPayload(result).assignment_errors || [];
      const calculationErrors = (result?.results || [])
        .flatMap(item => item.errors || []);
      const errors = [...new Set(
        [...reportErrors, ...calculationErrors]
          .map(error => error.message)
          .filter(Boolean),
      )];
      input.value = [
        "请补充本次指标预览中无法确定的团队归属关系：",
        ...errors.map(message => `- ${message}`),
        "",
        "我的确认是：",
      ].join("\n");
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
  });
}

function bindPerformanceReportDetailTabs() {
  document.querySelectorAll("[data-report-detail]").forEach(button => {
    button.addEventListener("click", () => {
      state.performanceReportDetailTab = button.dataset.reportDetail;
      document.querySelectorAll("[data-report-detail]").forEach(item => {
        const active = item.dataset.reportDetail === state.performanceReportDetailTab;
        item.classList.toggle("active", active);
        item.setAttribute("aria-selected", String(active));
      });
      document.querySelectorAll("[data-report-detail-panel]").forEach(panel => {
        panel.hidden = panel.dataset.reportDetailPanel !== state.performanceReportDetailTab;
      });
    });
  });
}

function showPerformanceReportPreview(result, controls, { target = $("#metric-preview-result") } = {}) {
  const scope = selectedPerformanceScope(result, controls.scopeKey);
  if (scope && !selectedPerformanceScope(result, state.performanceReportScopeKey)) {
    state.performanceReportScopeKey = String(scope.scope_key || "__total__");
    controls.scopeKey = state.performanceReportScopeKey;
  }
  const key = reportPreviewKey(
    state.selectedRule,
    controls.periodRef,
    controls.view,
    controls.anchorDate,
    controls.scopeKey,
  );
  state.metricPreviews.set(key, result);
  if (target) target.innerHTML = renderMetricPreviewResult(result, controls.scopeKey);
  const scopeSelect = $("#performance-report-scope");
  if (scopeSelect) {
    scopeSelect.innerHTML = renderPerformanceScopeOptions(result, controls.scopeKey);
    scopeSelect.disabled = !selectedPerformanceScope(result, controls.scopeKey);
  }
  document.querySelectorAll(".report-export").forEach(button => {
    button.disabled = !selectedPerformanceScope(result, controls.scopeKey);
  });
  const status = $("#performance-report-status");
  if (status) status.textContent = "预览已生成，可切换团队或导出。";
  bindMetricPreviewGuidance(result);
  bindPerformanceReportDetailTabs();
}

function syncPerformanceReportQuery() {
  const params = new URLSearchParams(location.search);
  params.set("page", "performance");
  if (state.selectedRule) params.set("rule", state.selectedRule);
  params.set("view", state.performanceReportView);
  if (state.performanceReportAnchorDate) {
    params.set("anchor_date", state.performanceReportAnchorDate);
  }
  if (state.performanceReportScopeKey) {
    params.set("scope", state.performanceReportScopeKey);
  }
  history.replaceState({}, "", `?${params.toString()}`);
}

async function requestPerformanceReport(rule, currentPeriod, button = null) {
  const controls = performanceReportControls(currentPeriod);
  if (!controls.periodRef || !controls.anchorDate) {
    showToast("请先选择考核周期和截止日期", true);
    return null;
  }
  const target = $("#metric-preview-result");
  if (target) {
    target.innerHTML = `<div class="metric-preview-loading"><span></span>
      <strong>正在生成${controls.view === "week" ? "周报" : "月报"}预览</strong>
      <small>只读取已校验底表，不会修改业务数据</small>
    </div>`;
  }
  const status = $("#performance-report-status");
  if (status) status.textContent = "正在按已确认口径计算…";
  setBusy(button, true, "生成中…");
  try {
    const result = await api(
      `rules/${encodeURIComponent(rule.rule_ref)}/report-preview`,
      {
        method: "POST",
        body: JSON.stringify({
          period_ref: controls.periodRef,
          view: controls.view,
          anchor_date: controls.anchorDate,
          scope_key: controls.scopeKey,
        }),
      },
    );
    showPerformanceReportPreview(result, controls, { target });
    syncPerformanceReportQuery();
    showToast(`${controls.view === "week" ? "周" : "月"}维度预览已更新`);
    return result;
  } catch (error) {
    if (target) {
      target.innerHTML = `<div class="report-empty-state report-error-state">
        <strong>这次预览没有生成</strong>
        <p>${escapeHtml(error.message)}</p>
        <button class="button" id="retry-performance-report" type="button">重新尝试</button>
      </div>`;
      $("#retry-performance-report")?.addEventListener("click", () => {
        requestPerformanceReport(rule, currentPeriod, $("#retry-performance-report"));
      });
    }
    if (status) status.textContent = "生成失败，请检查提示后重试。";
    document.querySelectorAll(".report-export").forEach(exportButton => {
      exportButton.disabled = true;
    });
    showToast(error.message, true);
    return null;
  } finally {
    setBusy(button, false);
  }
}

function performanceReportFileName(result, format) {
  const report = performanceReportPayload(result);
  const controls = performanceReportControls(
    state.periods.find(item => item.period_ref === state.selectedPeriod) || state.periods[0],
  );
  const scope = selectedPerformanceScope(result, controls.scopeKey);
  const cycle = controls.view === "week" ? "周报" : "月报";
  return `${reportScopeLabel(scope)}被告案件${cycle}.${format}`;
}

function bindPerformanceReportControls(rule, currentPeriod, savedPreview) {
  $("#performance-report-anchor")?.addEventListener("change", async event => {
    state.performanceReportAnchorDate = event.target.value;
    state.performanceReportDetailTab = "branches";
    syncPerformanceReportQuery();
    await requestPerformanceReport(rule, currentPeriod, $("#refresh-performance-report"));
  });
  $("#performance-report-scope")?.addEventListener("change", event => {
    const previousControls = performanceReportControls(currentPeriod);
    const result = state.metricPreviews.get(reportPreviewKey(
      rule.rule_ref,
      previousControls.periodRef,
      previousControls.view,
      previousControls.anchorDate,
      previousControls.scopeKey,
    )) || savedPreview;
    state.performanceReportScopeKey = event.target.value || "__total__";
    state.performanceReportDetailTab = "branches";
    const controls = performanceReportControls(currentPeriod);
    if (result) {
      state.metricPreviews.set(reportPreviewKey(
        rule.rule_ref,
        controls.periodRef,
        controls.view,
        controls.anchorDate,
        controls.scopeKey,
      ), result);
      showPerformanceReportPreview(result, controls);
    }
    syncPerformanceReportQuery();
  });
  $("#refresh-performance-report")?.addEventListener("click", event => {
    requestPerformanceReport(rule, currentPeriod, event.currentTarget);
  });
  document.querySelectorAll(".report-export").forEach(button => {
    button.addEventListener("click", () => {
      const controls = performanceReportControls(currentPeriod);
      const result = state.metricPreviews.get(reportPreviewKey(
        rule.rule_ref,
        controls.periodRef,
        controls.view,
        controls.anchorDate,
        controls.scopeKey,
      )) || savedPreview;
      if (!selectedPerformanceScope(result, controls.scopeKey)) {
        showToast("请先生成预览，再导出报告", true);
        return;
      }
      const format = button.dataset.format === "docx" ? "docx" : "xlsx";
      const query = new URLSearchParams({
        period_ref: controls.periodRef,
        view: controls.view,
        anchor_date: controls.anchorDate,
        scope_key: controls.scopeKey,
      });
      download(
        `rules/${encodeURIComponent(rule.rule_ref)}/report.${format}?${query.toString()}`,
        performanceReportFileName(result, format),
        button,
      );
    });
  });
  if (savedPreview) {
    bindMetricPreviewGuidance(savedPreview);
    bindPerformanceReportDetailTabs();
  }
}

function activateWorkspaceTab(targetId, { scroll = false } = {}) {
  const panels = [...document.querySelectorAll("[data-workspace-panel]")];
  if (!panels.some(panel => panel.id === targetId)) return;
  state.workspaceTab = targetId;
  for (const panel of panels) panel.hidden = panel.id !== targetId;
  document.querySelectorAll("[data-workspace-tab]").forEach(button => {
    const active = button.dataset.workspaceTab === targetId;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  if (scroll) {
    document.querySelector(".workspace-steps")?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }
}

async function finishSourceValidation(result, context, trigger) {
  state.workspaceTab = "source-files-section";
  if (["ready", "published"].includes(result.status)) {
    try {
      setBusy(trigger, true, "正在计算指标…");
      const preview = await api(
        `rules/${encodeURIComponent(context.ruleRef)}/preview-calculation`,
        {
          method: "POST",
          body: JSON.stringify({ period_ref: context.periodRef }),
        },
      );
      state.metricPreviews.set(
        previewKey(context.ruleRef, context.periodRef),
        preview,
      );
      state.workspaceTab = "metric-preview-section";
      showToast("底表校验通过，指标结果已经生成");
    } catch (error) {
      showToast(`底表已通过；指标暂未生成：${error.message}`, true);
    } finally {
      setBusy(trigger, false);
    }
  }
  await loadPerformanceBoardWorkspace(context.ruleRef);
}

async function confirmSourceMapping(form, context) {
  const mapping = Object.fromEntries(
    [...form.querySelectorAll("[data-field-key]")]
      .map(select => [select.dataset.fieldKey, select.value])
      .filter(([, value]) => value),
  );
  const result = await mutate(
    form.querySelector(".confirm-column-mapping"),
    () => api(`rules/${encodeURIComponent(context.ruleRef)}/source-files/${encodeURIComponent(context.sourceFileRef)}/validate`, {
      method: "POST",
      body: JSON.stringify({
        period_ref: context.periodRef,
        table_key: context.tableKey,
        column_mapping: mapping,
        confirm_mapping: true,
      }),
    }),
    "字段对应已保存，底表校验完成",
    false,
  );
  if (result.status === "mapping_required") {
    form.outerHTML = renderColumnMappingConfirmation(result);
    const replacement = context.card.querySelector(".column-mapping-form");
    replacement?.addEventListener("submit", event => {
      event.preventDefault();
      confirmSourceMapping(event.currentTarget, context);
    });
    return;
  }
  await finishSourceValidation(
    result,
    context,
    form.querySelector(".confirm-column-mapping"),
  );
}

async function validatePerformanceSource(button, rule) {
  const card = button.closest(".source-file-card");
  const context = {
    card,
    ruleRef: rule.rule_ref,
    sourceFileRef: card?.dataset.sourceFile || "",
    periodRef: card?.querySelector(".source-validation-period")?.value || "",
    tableKey: card?.querySelector(".source-validation-table")?.value || "",
  };
  const result = await mutate(button, () => api(
    `rules/${encodeURIComponent(context.ruleRef)}/source-files/${encodeURIComponent(context.sourceFileRef)}/validate`,
    {
      method: "POST",
      body: JSON.stringify({ period_ref: context.periodRef, table_key: context.tableKey }),
    },
  ), "底表字段已识别并完成校验", false);
  if (result.status === "mapping_required") {
    const slot = card.querySelector(".source-mapping-slot");
    slot.innerHTML = renderColumnMappingConfirmation(result);
    slot.scrollIntoView({ behavior: "smooth", block: "center" });
    slot.querySelector(".column-mapping-form")?.addEventListener("submit", event => {
      event.preventDefault();
      confirmSourceMapping(event.currentTarget, context);
    });
    return;
  }
  await finishSourceValidation(result, context, button);
}

async function bindPerformanceBoardWorkspace(rule, amendments, sourceFiles) {
  const selectedPeriod = state.periods.find(
    item => item.period_ref === state.selectedPeriod,
  ) || state.periods[0];
  const reportControls = performanceReportControls(selectedPeriod);
  const savedPreview = rule && selectedPeriod
    ? state.metricPreviews.get(reportPreviewKey(
      rule.rule_ref,
      selectedPeriod.period_ref,
      reportControls.view,
      reportControls.anchorDate,
      reportControls.scopeKey,
    ))
    : null;
  document.querySelectorAll("[data-workspace-tab]").forEach(button => {
    button.addEventListener("click", () => {
      activateWorkspaceTab(button.dataset.workspaceTab, { scroll: true });
    });
  });
  document.querySelectorAll(".switch-workspace-tab").forEach(button => {
    button.addEventListener("click", () => {
      activateWorkspaceTab(button.dataset.targetTab, { scroll: true });
    });
  });
  document.querySelectorAll(".quick-replace-source").forEach(button => {
    button.addEventListener("click", () => {
      activateWorkspaceTab("source-files-section", { scroll: true });
      document.querySelector("#rule-source-file-form input[type='file']")?.click();
    });
  });
  activateWorkspaceTab(
    state.workspaceTab || (rule ? "source-files-section" : "skill-section"),
  );
  $("#back-to-boards")?.addEventListener("click", () => {
    state.selectedRule = "";
    state.selectedDimension = "";
    state.workspaceTab = "";
    history.replaceState({}, "", "?page=performance");
    loadPerformance();
  });
  $("#empty-board-rule-upload-form")?.addEventListener("submit", event => uploadBoardRule(event));
  $("#replace-board-rule-form")?.addEventListener("submit", event => uploadBoardRule(event));
  $("#open-advanced-rule")?.addEventListener("click", () => showRuleDetail(rule.rule_ref));
  $("#interpret-board-rule")?.addEventListener("click", async event => {
    await mutate(event.currentTarget, () => api(`rules/${encodeURIComponent(rule.rule_ref)}/interpret`, { method: "POST" }), "Skill.md 已读取，请核对系统理解", false);
    await loadPerformanceBoardWorkspace(rule.rule_ref);
  });
  $("#new-period")?.addEventListener("click", renderPeriodForm);
  if (!rule) return;

  $("#rule-source-file-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const files = [...(event.currentTarget.elements.file.files || [])];
    if (!files.length) return showToast("请先选择至少一张底表", true);
    const currentPeriod = state.periods.find(
      item => item.period_ref === state.selectedPeriod,
    ) || state.periods[0];
    const willAutoValidate = Boolean(
      files.length === 1
      && rule.source_tables?.length === 1
      && currentPeriod
      && rule.rule_spec_hash,
    );
    let automaticValidation = null;
    let automaticContext = null;
    let automaticValidationError = "";
    await mutate(event.submitter, async () => {
      for (const file of files) {
        const data = new FormData();
        data.append("file", file);
        data.append("business_label", file.name.replace(/\.(xlsx|csv)$/i, ""));
        const uploaded = await api(
          `rules/${encodeURIComponent(rule.rule_ref)}/source-files/upload`,
          { method: "POST", body: data },
        );
        if (willAutoValidate) {
          automaticContext = {
            ruleRef: rule.rule_ref,
            sourceFileRef: uploaded.source_file_ref,
            periodRef: currentPeriod.period_ref,
            tableKey: rule.source_tables[0].key,
          };
          try {
            automaticValidation = await api(
              `rules/${encodeURIComponent(rule.rule_ref)}/source-files/${encodeURIComponent(uploaded.source_file_ref)}/validate`,
              {
                method: "POST",
                body: JSON.stringify({
                  period_ref: currentPeriod.period_ref,
                  table_key: rule.source_tables[0].key,
                }),
              },
            );
          } catch (error) {
            automaticValidationError = error.message;
          }
        }
      }
      return { status_label: automaticValidation ? "底表已上传并检查" : "底表已上传" };
    }, willAutoValidate ? "底表已上传并开始检查" : `${files.length} 张底表已上传`, false);
    state.workspaceTab = "source-files-section";
    if (automaticValidation?.status === "mapping_required") {
      await loadPerformanceBoardWorkspace(rule.rule_ref);
      const card = document.querySelector(
        `[data-source-file="${automaticContext.sourceFileRef}"]`,
      );
      const slot = card?.querySelector(".source-mapping-slot");
      if (slot) {
        slot.innerHTML = renderColumnMappingConfirmation(automaticValidation);
        slot.querySelector(".column-mapping-form")?.addEventListener("submit", mappingEvent => {
          mappingEvent.preventDefault();
          confirmSourceMapping(mappingEvent.currentTarget, { ...automaticContext, card });
        });
        slot.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    } else if (automaticValidation) {
      await finishSourceValidation(automaticValidation, automaticContext, event.submitter);
    } else {
      await loadPerformanceBoardWorkspace(rule.rule_ref);
      if (automaticValidationError) {
        showToast(`底表已上传；自动检查未完成：${automaticValidationError}`, true);
      }
    }
  });
  document.querySelectorAll(".abandon-rule-source-file").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("确认将这张底表移出当前板块？原始文件和操作记录仍会保留。")) return;
    await mutate(button, () => api(`rules/${encodeURIComponent(rule.rule_ref)}/source-files/${encodeURIComponent(button.dataset.sourceFile)}/abandon`, {
      method: "POST",
    }), "底表已移出", false);
    await loadPerformanceBoardWorkspace(rule.rule_ref);
  }));
  document.querySelectorAll(".validate-existing-rule-source-file").forEach(button => button.addEventListener("click", () => {
    validatePerformanceSource(button, rule);
  }));
  document.querySelectorAll(".view-source-data").forEach(button => button.addEventListener("click", () => {
    showValidatedSourceData(button.dataset.batch);
  }));
  document.querySelectorAll(".view-source-errors").forEach(button => button.addEventListener("click", () => {
    showValidatedSourceData(button.dataset.batch, 0, "error");
  }));
  document.querySelectorAll(".view-source-warnings").forEach(button => button.addEventListener("click", () => {
    showValidatedSourceData(button.dataset.batch, 0, "warning");
  }));
  document.querySelectorAll(".download-source-errors").forEach(button => button.addEventListener("click", event => {
    download(
      `batches/${encodeURIComponent(event.currentTarget.dataset.batch)}/errors.xlsx`,
      "底表问题数据.xlsx",
      event.currentTarget,
    );
  }));
  $("#submit-issue-resolutions")?.addEventListener("click", async event => {
    const resolutions = collectIssueResolutions(rule);
    if (!resolutions.length) return showToast("请至少填写一项确认结果", true);
    await mutate(event.currentTarget, () => api(`rules/${encodeURIComponent(rule.rule_ref)}/amendments/propose`, {
      method: "POST",
      body: JSON.stringify({
        expected_understanding_hash: rule.understanding_hash,
        instruction: "请严格根据业务人员逐项填写的确认结果更新完整规则草稿；未回答的问题继续保留，不得自行猜测。",
        issue_resolutions: resolutions,
      }),
    }), "答复已形成修改建议，请查看差异", false);
    await loadPerformanceBoardWorkspace(rule.rule_ref);
  });
  $("#rule-guidance-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const instruction = new FormData(event.currentTarget).get("instruction")?.trim() || "";
    if (!instruction) return showToast("请先填写需要修改的业务口径", true);
    let targetRule = rule;
    await mutate(event.submitter, async () => {
      if (rule.status === "active") {
        targetRule = await api(
          `rules/${encodeURIComponent(rule.rule_ref)}/working-copy`,
          { method: "POST" },
        );
      }
      return api(`rules/${encodeURIComponent(targetRule.rule_ref)}/amendments/propose`, {
        method: "POST",
        body: JSON.stringify({
          expected_understanding_hash: targetRule.understanding_hash,
          instruction,
          issue_resolutions: collectIssueResolutions(rule),
        }),
      });
    }, rule.status === "active"
      ? "新版本修改建议已生成；当前生效规则没有变化"
      : "修改建议已生成，请查看差异", false);
    state.selectedRule = targetRule.rule_ref;
    state.workspaceTab = "rule-detail-section";
    history.replaceState(
      {},
      "",
      `?${new URLSearchParams({ page: "performance", rule: targetRule.rule_ref }).toString()}`,
    );
    await loadPerformanceBoardWorkspace(targetRule.rule_ref);
  });
  document.querySelectorAll(".apply-rule-amendment").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("确认已经查看修改前后差异，并把这次建议应用到当前草稿？")) return;
    await mutate(button, () => api(`rules/${encodeURIComponent(rule.rule_ref)}/amendments/${encodeURIComponent(button.dataset.amendment)}/apply`, {
      method: "POST",
      body: JSON.stringify({ proposal_hash: button.dataset.hash, confirmed: true }),
    }), "修改建议已应用", false);
    await loadPerformanceBoardWorkspace(rule.rule_ref);
  }));
  $("#rule-validation-period")?.addEventListener("change", async event => {
    state.selectedPeriod = event.target.value;
    state.performanceReportAnchorDate = "";
    state.performanceReportScopeKey = "__total__";
    state.performanceReportDetailTab = "branches";
    state.workspaceTab = "metric-preview-section";
    await loadPerformanceBoardWorkspace(rule.rule_ref);
  });
  bindPerformanceReportControls(rule, selectedPeriod, savedPreview);
  $("#workspace-confirm-rule")?.addEventListener("click", async event => {
    const assignmentConfirmed = !rule.requires_assignment_confirmation || Boolean($("#assignment-confirmed")?.checked);
    if (!assignmentConfirmed) return showToast("请先核对并勾选团队与分公司的归属关系", true);
    if (!confirm("确认已核对 Skill、底表验证和指标结果，并启用这版规则？")) return;
    await mutate(event.currentTarget, () => api(`rules/${encodeURIComponent(rule.rule_ref)}/confirm`, {
      method: "POST",
      body: JSON.stringify({
        understanding_hash: event.currentTarget.dataset.hash,
        confirmed: true,
        assignment_confirmed: assignmentConfirmed,
      }),
    }), "规则版本已确认并启用", false);
    await loadPerformance();
  });
}

function renderRuleSection() {
  const rule = state.dashboard.rule_state;
  const caps = state.shell.identity.capabilities;
  const recentRules = state.rules.slice(0, 8);
  return `
    <div class="section-head">
      <div><h2>规则版本</h2><p>支持上传完整 Workbuddy 技能包，也可以直接上传单个 SKILL.md；系统会区分团队适用范围，并只生成待人工确认的理解草稿。</p></div>
      <span class="badge ${rule.configured ? "good" : "warn"}">${escapeHtml(rule.status_label)}</span>
    </div>
    <div class="grid">
      <article class="card span-7">
        <div class="card-head">
          <div><h3>${rule.configured ? escapeHtml(rule.skill_name) : "当前没有有效绩效规则"}</h3>
          <p>${rule.configured ? `规则版本 ${escapeHtml(rule.rule_version)} · 文件哈希 ${escapeHtml(rule.file_hash?.slice(0, 12))}…` : "明天拿到技能包后可在这里上传。没有规则时，系统不会伪造源表或计算结果。"}</p></div>
        </div>
        ${recentRules.length ? `<div class="divider"></div>
          <div class="stack compact-stack">${recentRules.map(item => `<div class="card" style="box-shadow:none">
            <div class="card-head"><div><strong>${escapeHtml(item.business_scope_name || item.skill_name)}</strong>
              <p>${escapeHtml(item.skill_name)} · ${escapeHtml(item.status_label)} · ${formatTime(item.created_at)}</p></div>
              <span class="badge ${statusClass(item.status)}">${escapeHtml(item.status_label)}</span></div>
            <div class="toolbar" style="margin-top:10px">
              <button class="button rule-detail" data-ref="${escapeHtml(item.rule_ref)}">打开规则工作台</button>
              ${caps.performance_upload && ["uploaded","understanding_draft"].includes(item.status) ? `<button class="button interpret-rule" data-ref="${escapeHtml(item.rule_ref)}">读取并理解</button>` : ""}
              ${item.is_current ? `<span class="badge good">当前有效</span>` : ""}
            </div>
          </div>`).join("")}</div>` : ""}
      </article>
      <article class="card span-5">
        <h3>上传新的技能包</h3>
        <p class="subtle">支持 zip 技能包或单个 SKILL.md。zip 中必须且只能包含一个 SKILL.md；可附模板、说明、docx 参考文档和 rule-spec.json。参考文档只读取文字，网页不会执行脚本、宏、Python 或 SQL。</p>
        <form id="rule-upload-form" class="stack">
          <label>Workbuddy 技能包 / SKILL.md<input name="file" type="file" accept=".zip,.md" required ${caps.performance_upload ? "" : "disabled"} /></label>
          <label>负责板块<input name="business_scope_name" maxlength="256" placeholder="例如：被告案件、诉讼收款、法务三部专项" required ${caps.performance_upload ? "" : "disabled"} /></label>
          <label>适用周期（可选）
            <select name="period_ref"><option value="">通用规则</option>${state.periods.map(item => `<option value="${escapeHtml(item.period_ref)}">${escapeHtml(item.label)}</option>`).join("")}</select>
          </label>
          <button class="button primary" type="submit" ${caps.performance_upload ? "" : "disabled"}>上传并安全检查</button>
        </form>
      </article>
    </div>`;
}

function renderPeriod(period) {
  if (!period) return "";
  const packages = period.packages || (period.package?.rule_configured ? [period.package] : []);
  if (!packages.length) return `
    <article class="card">
      <div class="card-head"><div><h2>${escapeHtml(period.label)}</h2><p>${escapeHtml(period.period_type_label)} · ${escapeHtml(period.starts_on)} 至 ${escapeHtml(period.ends_on)}</p></div></div>
      <div class="empty"><div><strong>本周期还没有已启用的负责板块</strong>上传并确认对应 Skill 后，每个板块会分别显示自己的底表和试算入口，互不覆盖。</div></div>
    </article>`;
  return `<div class="stack">${packages.map(pack => renderPerformancePackage(period, pack)).join("")}</div>`;
}

function renderPerformancePackage(period, pack) {
  const percent = pack.required_table_count ? Math.round(pack.passed_count / pack.required_table_count * 100) : 0;
  return `
    <article class="card">
      <div class="card-head">
        <div><h2>${escapeHtml(pack.business_scope_name || "未命名负责板块")}</h2><p>${escapeHtml(period.label)} · ${escapeHtml(period.starts_on)} 至 ${escapeHtml(period.ends_on)}</p></div>
        <span class="badge ${pack.can_calculate ? "good" : "warn"}">${pack.can_calculate ? "可以试算" : escapeHtml(pack.message || "数据尚未准备完成")}</span>
      </div>
      <div class="metric-row">
        <div class="metric"><span>必需表</span><strong>${pack.required_table_count}</strong></div>
        <div class="metric"><span>已上传</span><strong>${pack.uploaded_count}</strong></div>
        <div class="metric"><span>校验通过</span><strong>${pack.passed_count}</strong></div>
        <div class="metric"><span>存在错误</span><strong>${pack.error_count}</strong></div>
        <div class="metric"><span>尚未上传</span><strong>${pack.missing_count}</strong></div>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
      <p class="subtle">${pack.rule_configured ? `使用规则版本：${escapeHtml(pack.rule_version)}` : "规则文件尚未配置，暂不可计算"}</p>
      <div class="divider"></div>
      ${pack.tables.length ? `<div class="grid">${pack.tables.map(table => renderTableCard(period, pack, table)).join("")}</div>` :
        `<div class="empty"><div><strong>还不能展示必需表格</strong>上传并确认 Workbuddy 技能包后，系统会按 SKILL.md 中的实际表名展示。</div></div>`}
      <div class="toolbar" style="margin-top:18px">
        <button class="button primary trial-calculate" data-period="${escapeHtml(period.period_ref)}" data-rule="${escapeHtml(pack.rule_ref)}" ${pack.can_calculate && state.shell.identity.capabilities.performance_upload ? "" : "disabled"}>试算本板块</button>
        <span class="subtle">${pack.can_calculate ? "试算不会直接发布正式结果。" : "所有必需表发布后才能试算。"}</span>
      </div>
    </article>`;
}

function renderTableCard(period, pack, table) {
  const caps = state.shell.identity.capabilities;
  return `
    <div class="card span-6" style="box-shadow:none">
      <div class="card-head"><div><h3>${escapeHtml(table.table_name)}</h3><p>${escapeHtml(table.purpose || "规则要求的源表")}</p></div>
        <span class="badge ${statusClass(table.status)}">${escapeHtml(table.status_label)}</span></div>
      <div class="metric-row" style="grid-template-columns:repeat(3,1fr)">
        <div class="metric"><span>${table.status === "published" ? "当前正式版本" : "本次版本"}</span><strong>${table.version || "—"}</strong></div>
        <div class="metric"><span>数据行</span><strong>${table.row_count}</strong></div>
        <div class="metric"><span>错误</span><strong>${table.error_count}</strong></div>
      </div>
      ${table.status !== "published" && table.current_version ? `<p class="subtle">当前仍用于试算的是第 ${table.current_version} 版正式数据。</p>` : ""}
      <p class="subtle">${table.uploaded_by ? `上传：${escapeHtml(table.uploaded_by)} · ${formatTime(table.uploaded_at)}` : "尚未上传"}</p>
      <div class="toolbar">
        <button class="button template-download" data-type="performance" data-table="${escapeHtml(table.table_key)}" data-period="${escapeHtml(period.period_ref)}" data-rule="${escapeHtml(pack.rule_ref)}">下载模板</button>
        <label class="button" style="cursor:pointer">选择文件<input class="performance-file" data-period="${escapeHtml(period.period_ref)}" data-rule="${escapeHtml(pack.rule_ref)}" data-table="${escapeHtml(table.table_key)}" type="file" accept=".xlsx,.csv" hidden ${caps.performance_upload ? "" : "disabled"} /></label>
        ${table.status === "ready" && caps.publish ? `<button class="button primary performance-publish" data-batch="${escapeHtml(table.batch_no)}">确认发布</button>` : ""}
        ${table.batch_no ? `<button class="button performance-preview" data-batch="${escapeHtml(table.batch_no)}">查看数据</button>` : ""}
      </div>
    </div>`;
}

function bindPerformanceEvents(period) {
  $("#new-period")?.addEventListener("click", renderPeriodForm);
  $("#period-select")?.addEventListener("change", event => {
    state.selectedPeriod = event.target.value;
    loadPerformance();
  });
  $("#rule-upload-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.submitter;
    const data = new FormData(event.currentTarget);
    await mutate(button, () => api("rules/upload", { method: "POST", body: data }), "技能包已上传并完成安全检查");
  });
  document.querySelectorAll(".interpret-rule").forEach(button => button.addEventListener("click", async event => {
    const reference = event.currentTarget.dataset.ref;
    await mutate(event.currentTarget, () => api(`rules/${reference}/interpret`, { method: "POST" }), "规则理解草稿已生成", false);
    await showRuleDetail(reference);
    await loadPerformance();
  }));
  document.querySelectorAll(".rule-detail").forEach(button => button.addEventListener("click", () => showRuleDetail(button.dataset.ref)));
  document.querySelectorAll(".template-download").forEach(button => button.addEventListener("click", () => {
    download(`templates/${button.dataset.type}?table_key=${encodeURIComponent(button.dataset.table || "")}&period_ref=${encodeURIComponent(button.dataset.period || "")}&rule_ref=${encodeURIComponent(button.dataset.rule || "")}`, `${button.dataset.table || "数据"}-模板.xlsx`, button);
  }));
  document.querySelectorAll(".performance-file").forEach(input => input.addEventListener("change", async event => {
    if (!event.target.files[0]) return;
    const data = new FormData();
    data.append("file", event.target.files[0]);
    data.append("period_ref", event.target.dataset.period);
    data.append("table_key", event.target.dataset.table);
    data.append("rule_ref", event.target.dataset.rule || "");
    const label = event.target.closest("label");
    const result = await mutate(label, () => api("performance/tables/upload", { method: "POST", body: data }), "文件已校验并进入待发布批次", false);
    await showBatch(result.batch_no);
    await loadPerformance();
  }));
  document.querySelectorAll(".performance-publish").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("确认将该源表版本发布为本考核周期的当前有效数据？旧版本会保留，旧试算会标记为需要重算。")) return;
    await mutate(button, () => api(`performance/tables/${button.dataset.batch}/publish`, { method: "POST" }), "绩效源表已发布");
  }));
  document.querySelectorAll(".performance-preview").forEach(button => button.addEventListener("click", () => showBatch(button.dataset.batch)));
  document.querySelectorAll(".trial-calculate").forEach(button => button.addEventListener("click", async event => {
    const result = await mutate(event.currentTarget, () => api("calculations/trial", {
      method: "POST", body: JSON.stringify({
        period_ref: event.currentTarget.dataset.period,
        rule_ref: event.currentTarget.dataset.rule,
      }),
    }), "试算完成", false);
    await showCalculation(result);
  }));
}

function scopeEditRow(item = {}) {
  return `<div class="rule-edit-row scope-edit-row">
    <input data-field="scope_name" maxlength="256" placeholder="板块或团队名称" value="${escapeHtml(item.scope_name || item.team_name || item.name || "")}" />
    <input data-field="scope_key" maxlength="128" placeholder="稳定标识（可稍后补）" value="${escapeHtml(item.scope_key || item.team_key || "")}" />
    <input data-field="description" maxlength="1000" placeholder="适用说明" value="${escapeHtml(item.description || item.purpose || "")}" />
    <button class="button danger remove-rule-row" type="button">移除</button>
  </div>`;
}

function targetEditRow(item = {}) {
  return `<div class="rule-edit-row target-edit-row">
    <input data-field="metric_name" maxlength="256" placeholder="指标名称" value="${escapeHtml(businessMetricName(item.metric_name || item.name || ""))}" />
    <input data-field="scope_name" maxlength="256" placeholder="适用板块" value="${escapeHtml(item.scope_name || item.team_name || "")}" />
    <select data-field="comparison" aria-label="目标判断方向">
      <option value="" ${!item.comparison ? "selected" : ""}>判断方向待确认</option>
      <option value="at_most" ${item.comparison === "at_most" ? "selected" : ""}>不高于</option>
      <option value="at_least" ${item.comparison === "at_least" ? "selected" : ""}>不低于</option>
      <option value="equal" ${item.comparison === "equal" ? "selected" : ""}>等于</option>
    </select>
    <input data-field="target_value" maxlength="128" placeholder="目标值" value="${escapeHtml(item.target_value ?? item.value ?? "")}" />
    <input data-field="unit" maxlength="64" placeholder="单位" value="${escapeHtml(item.unit || "")}" />
    <input data-field="effective_period" maxlength="256" placeholder="生效周期" value="${escapeHtml(item.effective_period || item.period || "")}" />
    <button class="button danger remove-rule-row" type="button">移除</button>
  </div>`;
}

function amendmentDiffList(items) {
  if (!items?.length) return `<span class="subtle">无</span>`;
  return `<ul>${items.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function renderRuleAmendment(item) {
  return `<article class="card amendment-card" style="box-shadow:none">
    <div class="card-head"><div>
      <strong>第 ${item.amendment_no} 次修改 · ${escapeHtml(item.change_kind_label)}</strong>
      <p>${formatTime(item.created_at)} · ${escapeHtml(item.status_label)}</p>
    </div><span class="badge ${item.status === "applied" ? "good" : item.status === "proposed" ? "warn" : "info"}">${escapeHtml(item.status_label)}</span></div>
    ${item.instruction ? `<p style="margin-top:12px">${escapeHtml(item.instruction)}</p>` : ""}
    <details ${item.status === "proposed" ? "open" : ""}>
      <summary>查看修改前后差异</summary>
      <div class="stack" style="margin-top:10px">
        ${(item.changes || []).length ? item.changes.map(change => `<div class="diff-block">
          <strong>${escapeHtml(change.section)}</strong>
          <div class="diff-columns">
            <div><span class="subtle">修改前</span>${amendmentDiffList(change.before)}</div>
            <div><span class="subtle">修改后</span>${amendmentDiffList(change.after)}</div>
          </div>
        </div>`).join("") : `<div class="notice">没有识别到可应用的变化，请补充更明确的说明。</div>`}
      </div>
    </details>
    <p class="subtle" style="margin-top:10px">
      固定计算结构：${item.validation?.fixed_program_ready ? "已形成" : "尚未形成"} ·
      剩余待确认：${item.validation?.remaining_questions ?? "—"} 项 ·
      原文与补充证据：${item.validation?.evidence_count ?? "—"} 条
    </p>
    ${item.can_apply ? `<button class="button primary apply-rule-amendment" type="button"
      data-amendment="${escapeHtml(item.amendment_ref)}" data-hash="${escapeHtml(item.proposal_hash)}">应用这次修改</button>` : ""}
  </article>`;
}

function renderRuleSourceFile(item, sourceFileEditable, rule, periods) {
  const profile = item.inspection || {};
  const canValidate = sourceFileEditable && rule.rule_spec_hash && periods.length && rule.source_tables?.length;
  return `<article class="card source-file-card" data-source-file="${escapeHtml(item.source_file_ref)}" style="box-shadow:none">
    <div class="card-head">
      <div>
        <strong>${escapeHtml(item.business_label || item.file_name)}</strong>
        <p>${escapeHtml(item.file_name)} · ${(Number(item.size_bytes || 0) / 1024 / 1024).toFixed(2)} MB · ${formatTime(item.uploaded_at)}</p>
      </div>
      <span class="badge ${profile.usable_sheet_count ? "good" : "bad"}">${profile.usable_sheet_count ? `识别到 ${profile.total_data_rows || 0} 行数据` : "没有可用数据行"}</span>
    </div>
    ${(profile.warnings || []).length ? `<div class="source-warning-list">${profile.warnings.map(message => `<div class="notice">${escapeHtml(message)}</div>`).join("")}</div>` : ""}
    ${(profile.sheets || []).map(sheet => `<details style="margin-top:10px">
      <summary>${escapeHtml(sheet.sheet_name)} · ${sheet.row_count} 行 · ${sheet.column_count} 列</summary>
      <div class="column-chip-list">
        ${(sheet.columns || []).map(column => `<span class="badge info">第 ${column.position} 列 · ${escapeHtml(column.header)} · ${escapeHtml(columnTypeLabel(column.inferred_type))}</span>`).join("")}
      </div>
    </details>`).join("")}
    <p class="subtle" style="margin-top:10px">底表结构只用于规则核对；这里不展示业务数据值，也不会写入正式绩效。</p>
    ${canValidate ? `<div class="reuse-source-file-panel">
      <strong>直接用这张底表验证</strong>
      <div class="form-grid" style="margin-top:8px">
        <label>验证周期<select class="source-validation-period">
          ${periods.map(period => `<option value="${escapeHtml(period.period_ref)}">${escapeHtml(period.label)}</option>`).join("")}
        </select></label>
        <label>对应规则表<select class="source-validation-table">
          ${(rule.source_tables || []).map(table => `<option value="${escapeHtml(table.key || "")}">${escapeHtml(table.name || table.key || "源表")}</option>`).join("")}
        </select></label>
      </div>
      <button class="button primary validate-existing-rule-source-file" type="button">校验并用于指标验证</button>
      <span class="subtle">无需重复上传；只生成待核对批次，不会自动发布。</span>
    </div>` : ""}
    <div class="source-mapping-slot"></div>
    ${sourceFileEditable && item.can_abandon ? `<button class="button danger abandon-rule-source-file" type="button" data-source-file="${escapeHtml(item.source_file_ref)}">移出当前规则核对</button>` : ""}
  </article>`;
}

function renderRuleWorkspace(rule, amendments, sourceFiles) {
  const caps = state.shell.identity.capabilities;
  const editable = caps.performance_upload && ["understanding_draft", "awaiting_confirmation"].includes(rule.status);
  const sourceFileEditable = caps.performance_upload && ["uploaded", "understanding_draft", "awaiting_confirmation"].includes(rule.status);
  const activeSourceFiles = (sourceFiles || []).filter(item => item.status === "active");
  const resolutionMap = new Map((rule.issue_resolutions || []).map(item => [item.issue, item.response]));
  const periods = state.periods || [];
  return `<section class="rule-workspace">
    <div class="workspace-header">
      <div>
        <p class="eyebrow">规则工作台</p>
        <h3>${escapeHtml(rule.business_scope_name || "尚未填写负责板块")}</h3>
        <p class="subtle">上传 Skill → 系统理解 → 编辑或沟通修改 → 验证底表 → 发布管理员确认 → 试算</p>
      </div>
      <span class="badge ${statusClass(rule.status)}">草稿第 ${rule.draft_revision || 1} 版</span>
    </div>
    <div class="notice" style="margin:12px 0">
      模型只负责理解和生成修改建议；修改建议不会自动生效。必须人工查看差异并应用，固定程序校验通过后才能确认规则。
    </div>
    <details open>
      <summary><strong>先上传现有底表</strong>：不需要改成模板，先让系统核对真实工作表和表头</summary>
      <div class="stack" style="margin-top:14px">
        <form id="rule-source-file-form" class="source-file-upload">
          <label>负责板块的现有底表
            <input name="file" type="file" accept=".xlsx,.csv" multiple required ${sourceFileEditable ? "" : "disabled"} />
          </label>
          <button class="button primary" type="submit" ${sourceFileEditable ? "" : "disabled"}>上传并读取结构</button>
        </form>
        <p class="subtle">支持一次选择多张现有大表。系统保存原始文件和哈希，只读取工作表、表头、列位置、数据类型和行数，不执行公式或宏。</p>
        ${activeSourceFiles.length ? `<div class="stack">${activeSourceFiles.map(item => renderRuleSourceFile(item, sourceFileEditable, rule, periods)).join("")}</div>` :
          `<div class="empty compact-empty">还没有用于规则核对的底表。可以先上传原有文件，再让系统结合真实表头校准 Skill。</div>`}
        ${editable && activeSourceFiles.length ? `<div class="toolbar">
          <button class="button" id="calibrate-rule-from-source-files" type="button">根据这些底表校准规则</button>
          <span class="subtle">系统会生成一份修改建议；必须查看修改前后差异并人工应用。</span>
        </div>` : rule.status === "uploaded" && activeSourceFiles.length ? `<div class="notice">底表结构已经读取。请先点击“读取并理解规则与参考资料”，随后即可生成校准建议。</div>` : ""}
      </div>
    </details>
    <details open>
      <summary><strong>直接编辑</strong>：板块、适用范围、目标和待确认事项答复</summary>
      <form id="rule-draft-form" class="stack" style="margin-top:14px">
        <div class="form-grid">
          <label>负责板块<input name="business_scope_name" maxlength="256" value="${escapeHtml(rule.business_scope_name === "尚未填写" ? "" : rule.business_scope_name || "")}" ${editable ? "" : "disabled"} /></label>
          <label>适用周期<input name="applicable_period" maxlength="256" value="${escapeHtml(rule.applicable_period || "")}" ${editable ? "" : "disabled"} /></label>
        </div>
        <label>规则摘要<textarea name="rule_summary" maxlength="4000" rows="3" ${editable ? "" : "disabled"}>${escapeHtml(rule.rule_summary || "")}</textarea></label>
        <div>
          <div class="card-head"><strong>适用板块</strong><button class="button" id="add-rule-scope" type="button" ${editable ? "" : "disabled"}>新增板块</button></div>
          <div id="scope-edit-list" class="stack compact-stack" style="margin-top:8px">
            ${(rule.applicability_scopes || []).map(scopeEditRow).join("") || scopeEditRow()}
          </div>
        </div>
        <div>
          <div class="card-head"><strong>指标目标</strong><button class="button" id="add-rule-target" type="button" ${editable ? "" : "disabled"}>新增目标</button></div>
          <div id="target-edit-list" class="stack compact-stack" style="margin-top:8px">
            ${(rule.target_versions || []).map(targetEditRow).join("") || targetEditRow()}
          </div>
        </div>
        <div>
          <strong>待确认事项答复</strong>
          <div class="stack" style="margin-top:8px">
            ${(rule.unresolved || []).length ? rule.unresolved.map((issue, index) => `<label class="issue-editor">
              <span>${index + 1}. ${escapeHtml(issue)}</span>
              <textarea class="issue-resolution" data-index="${index}" maxlength="4000" rows="2"
                placeholder="填写你确认的业务口径；保存后可让系统据此生成修改建议" ${editable ? "" : "disabled"}>${escapeHtml(resolutionMap.get(issue) || "")}</textarea>
            </label>`).join("") : `<p class="success-text">当前没有待确认事项。</p>`}
          </div>
        </div>
        <div class="toolbar">
          <button class="button primary" type="submit" ${editable ? "" : "disabled"}>保存草稿</button>
          ${(rule.unresolved || []).length ? `<button class="button" id="submit-issue-resolutions" type="button" ${editable ? "" : "disabled"}>提交答复并生成修改建议</button>` : ""}
          <span class="subtle">直接编辑不会修改可执行公式；涉及计算的内容需在下方生成修改建议。</span>
        </div>
      </form>
    </details>
    <details open style="margin-top:18px">
      <summary><strong>沟通修改</strong>：用中文告诉系统哪里需要调整</summary>
      <form id="rule-guidance-form" class="stack" style="margin-top:14px">
        <label>修改说明<textarea name="instruction" maxlength="8000" rows="5"
          placeholder="例如：新增案件按考核周期对应的自然月统计；去年同期为0时显示不可比，不按0%处理。" ${editable ? "" : "disabled"}></textarea></label>
        <div class="toolbar">
          <button class="button primary" type="submit" ${editable ? "" : "disabled"}>生成修改建议</button>
          <span class="subtle">系统会结合上方逐项答复生成完整新草稿，不会直接覆盖当前版本。</span>
        </div>
      </form>
    </details>
    <details open style="margin-top:18px">
      <summary><strong>修改记录</strong></summary>
      <div class="stack" style="margin-top:12px">
        ${(amendments || []).length ? amendments.map(renderRuleAmendment).join("") : `<div class="empty compact-empty">还没有修改记录。</div>`}
      </div>
    </details>
    <details open style="margin-top:18px">
      <summary><strong>验证底表</strong>：在规则确认前用真实数据校验字段与结果</summary>
      <div style="margin-top:14px">
        ${periods.length ? `<label>验证周期<select id="rule-validation-period">
          ${periods.map(item => `<option value="${escapeHtml(item.period_ref)}">${escapeHtml(item.label)}</option>`).join("")}
        </select></label>` : `<div class="notice">请先创建考核周期，再上传验证底表。</div>`}
        ${(rule.source_tables || []).length ? `<div class="grid" style="margin-top:12px">
          ${rule.source_tables.map(table => `<article class="card span-6" style="box-shadow:none">
            <strong>${escapeHtml(table.name || table.key || "源表")}</strong>
            <p class="subtle">${escapeHtml(table.purpose || "")}</p>
            ${table.allow_extra_columns ? `<p class="success-text">可以直接上传原业务表；规则未使用的 ERP 列可以保留。</p>` : ""}
            <label class="button" style="cursor:pointer">上传验证底表
              <input class="rule-validation-file" type="file" hidden accept=".xlsx,.csv"
                data-table="${escapeHtml(table.key || "")}" ${editable && periods.length && rule.rule_spec_hash ? "" : "disabled"} />
            </label>
          </article>`).join("")}
        </div>` : `<div class="empty compact-empty">系统还没有确认源表结构，请先通过沟通修改完善。</div>`}
        <div class="toolbar" style="margin-top:12px">
          <button class="button" id="run-rule-preview" type="button"
            ${editable && periods.length && rule.rule_spec_hash ? "" : "disabled"}>运行验证并查看指标完成情况</button>
          <span class="subtle">验证结果不写入正式绩效，不会自动发布底表。</span>
        </div>
      </div>
    </details>
    ${caps.publish && rule.status === "awaiting_confirmation" ? `<div class="toolbar final-rule-confirm">
      ${rule.requires_assignment_confirmation ? `<label class="confirmation-check">
        <input id="assignment-confirmed" type="checkbox" />
        我已逐步核对团队或人员归属链；没有用底表中的其他团队列替代 Skill 规定的映射关系
      </label>` : ""}
      <button class="button primary" id="workspace-confirm-rule" data-hash="${escapeHtml(rule.understanding_hash)}">确认规则版本</button>
      <span class="subtle">确认规则与正式发布绩效结果仍然分开。</span>
    </div>` : ""}
  </section>`;
}

function collectIssueResolutions(rule) {
  return [...document.querySelectorAll(".issue-resolution")].map(input => {
    const issue = (rule.unresolved || [])[Number(input.dataset.index)];
    return { issue, response: input.value.trim() };
  }).filter(item => item.issue && item.response);
}

async function bindRuleWorkspace(rule) {
  const ruleRef = rule.rule_ref;
  $("#rule-source-file-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const files = [...(event.currentTarget.elements.file.files || [])];
    if (!files.length) {
      showToast("请先选择至少一张底表", true);
      return;
    }
    await mutate(event.submitter, async () => {
      const uploaded = [];
      for (const file of files) {
        const data = new FormData();
        data.append("file", file);
        data.append("business_label", file.name.replace(/\.(xlsx|csv)$/i, ""));
        uploaded.push(await api(`rules/${encodeURIComponent(ruleRef)}/source-files/upload`, {
          method: "POST", body: data,
        }));
      }
      return uploaded;
    }, `${files.length} 张底表已读取结构`, false);
    await showRuleDetail(ruleRef);
  });
  drawerContent.querySelectorAll(".abandon-rule-source-file").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("确认把这张底表移出当前规则核对？原始文件和操作记录仍会保留。")) return;
    await mutate(button, () => api(`rules/${encodeURIComponent(ruleRef)}/source-files/${encodeURIComponent(button.dataset.sourceFile)}/abandon`, {
      method: "POST",
    }), "底表已移出当前规则核对", false);
    await showRuleDetail(ruleRef);
  }));
  drawerContent.querySelectorAll(".validate-existing-rule-source-file").forEach(button => button.addEventListener("click", async () => {
    await validatePerformanceSource(button, rule);
  }));
  $("#calibrate-rule-from-source-files")?.addEventListener("click", async event => {
    await mutate(event.currentTarget, () => api(`rules/${encodeURIComponent(ruleRef)}/amendments/propose`, {
      method: "POST",
      body: JSON.stringify({
        expected_understanding_hash: rule.understanding_hash,
        instruction: "请根据网页已读取的现有底表结构，校准源表名称、工作表、真实表头、列位置和数据类型；如底表与 SKILL.md 的列说明或计算口径冲突，请保留为待确认事项，不要猜测。",
        issue_resolutions: collectIssueResolutions(rule),
      }),
    }), "底表校准建议已生成，请查看前后差异", false);
    await showRuleDetail(ruleRef);
  });
  $("#add-rule-scope")?.addEventListener("click", () => {
    $("#scope-edit-list").insertAdjacentHTML("beforeend", scopeEditRow());
  });
  $("#add-rule-target")?.addEventListener("click", () => {
    $("#target-edit-list").insertAdjacentHTML("beforeend", targetEditRow());
  });
  drawerContent.querySelectorAll(".remove-rule-row").forEach(button => button.addEventListener("click", () => {
    button.closest(".rule-edit-row")?.remove();
  }));
  $("#rule-draft-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const scopes = [...document.querySelectorAll(".scope-edit-row")].map(row =>
      Object.fromEntries([...row.querySelectorAll("[data-field]")].map(input => [input.dataset.field, input.value.trim()])),
    ).filter(item => item.scope_name);
    const targets = [...document.querySelectorAll(".target-edit-row")].map(row =>
      Object.fromEntries([...row.querySelectorAll("[data-field]")].map(input => [input.dataset.field, input.value.trim()])),
    ).filter(item => item.metric_name);
    const body = {
      expected_understanding_hash: rule.understanding_hash,
      business_scope_name: values.get("business_scope_name") || "",
      rule_summary: values.get("rule_summary") || "",
      applicable_period: values.get("applicable_period") || "",
      applicability_scopes: scopes,
      target_versions: targets,
      issue_resolutions: collectIssueResolutions(rule),
    };
    await mutate(event.submitter, () => api(`rules/${encodeURIComponent(ruleRef)}/draft`, {
      method: "PUT", body: JSON.stringify(body),
    }), "规则草稿已保存", false);
    await loadPerformance();
    await showRuleDetail(ruleRef);
  });
  $("#submit-issue-resolutions")?.addEventListener("click", async event => {
    const resolutions = collectIssueResolutions(rule);
    if (!resolutions.length) {
      showToast("请至少填写一项待确认事项的答复", true);
      return;
    }
    await mutate(event.currentTarget, () => api(`rules/${encodeURIComponent(ruleRef)}/amendments/propose`, {
      method: "POST",
      body: JSON.stringify({
        expected_understanding_hash: rule.understanding_hash,
        instruction: "请严格根据业务人员对待确认事项的逐项答复更新完整规则草稿；没有答复的事项继续保留，不得自行猜测。",
        issue_resolutions: resolutions,
      }),
    }), "答复已形成修改建议，请查看前后差异", false);
    await showRuleDetail(ruleRef);
  });
  $("#rule-guidance-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const instruction = new FormData(event.currentTarget).get("instruction")?.trim() || "";
    if (!instruction) {
      showToast("请先填写需要修改的业务口径", true);
      return;
    }
    await mutate(event.submitter, () => api(`rules/${encodeURIComponent(ruleRef)}/amendments/propose`, {
      method: "POST",
      body: JSON.stringify({
        expected_understanding_hash: rule.understanding_hash,
        instruction,
        issue_resolutions: collectIssueResolutions(rule),
      }),
    }), "修改建议已生成，请查看前后差异", false);
    await showRuleDetail(ruleRef);
  });
  drawerContent.querySelectorAll(".apply-rule-amendment").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("确认已经查看修改前后差异，并把这次建议应用到当前草稿？这不会启用规则，也不会计算或发布绩效。")) return;
    await mutate(button, () => api(`rules/${encodeURIComponent(ruleRef)}/amendments/${encodeURIComponent(button.dataset.amendment)}/apply`, {
      method: "POST",
      body: JSON.stringify({ proposal_hash: button.dataset.hash, confirmed: true }),
    }), "修改建议已应用到草稿", false);
    await loadPerformance();
    await showRuleDetail(ruleRef);
  }));
  drawerContent.querySelectorAll(".rule-validation-file").forEach(input => input.addEventListener("change", async event => {
    const file = event.target.files?.[0];
    if (!file) return;
    const periodRef = $("#rule-validation-period")?.value || "";
    const data = new FormData();
    data.append("file", file);
    data.append("period_ref", periodRef);
    data.append("table_key", event.target.dataset.table);
    data.append("rule_ref", ruleRef);
    const result = await mutate(event.target.closest("label"), () => api("performance/tables/upload", {
      method: "POST", body: data,
    }), "验证底表已完成字段与数据校验", false);
    await showBatch(result.batch_no);
  }));
  $("#run-rule-preview")?.addEventListener("click", async event => {
    const periodRef = $("#rule-validation-period")?.value || "";
    const result = await mutate(event.currentTarget, () => api(`rules/${encodeURIComponent(ruleRef)}/preview-calculation`, {
      method: "POST", body: JSON.stringify({ period_ref: periodRef }),
    }), "验证计算完成", false);
    await showRulePreview(ruleRef, result);
  });
  $("#workspace-confirm-rule")?.addEventListener("click", async event => {
    const assignmentConfirmed = !rule.requires_assignment_confirmation || Boolean($("#assignment-confirmed")?.checked);
    if (!assignmentConfirmed) {
      showToast("请先核对并勾选团队或人员归属链", true);
      return;
    }
    if (!confirm("确认已经核对 Skill、修改记录、底表验证和指标结果，并将该草稿确认为可用规则版本？")) return;
    await mutate(event.currentTarget, () => api(`rules/${encodeURIComponent(ruleRef)}/confirm`, {
      method: "POST",
      body: JSON.stringify({
        understanding_hash: event.currentTarget.dataset.hash,
        confirmed: true,
        assignment_confirmed: assignmentConfirmed,
      }),
    }), "规则版本已确认并启用", false);
    closeDrawer();
    await loadPerformance();
  });
}

async function showRuleDetail(ruleRef) {
  try {
    const [rule, amendments, sourceFiles] = await Promise.all([
      api(`rules/${encodeURIComponent(ruleRef)}`),
      api(`rules/${encodeURIComponent(ruleRef)}/amendments`),
      api(`rules/${encodeURIComponent(ruleRef)}/source-files`),
    ]);
    drawerContent.innerHTML = `
      <p class="eyebrow">规则理解与原文证据</p>
      <h2>${escapeHtml(rule.skill_name || "绩效规则")}</h2>
      <p><span class="badge ${statusClass(rule.status)}">${escapeHtml(rule.status_label)}</span></p>
      <p class="subtle">规则版本 ${escapeHtml(rule.rule_version)} · 适用周期 ${escapeHtml(rule.applicable_period)} · 来源 ${escapeHtml(rule.source)}</p>
      <div class="notice" style="margin:14px 0">${escapeHtml(rule.activation_notice)}</div>
      ${renderRuleWorkspace(rule, amendments, sourceFiles)}
      <div class="divider"></div>
      <h3>规则摘要</h3><p>${escapeHtml(rule.rule_summary || "尚未形成摘要")}</p>
      <h3>源表与稳定关联字段</h3>
      ${rule.source_tables?.length ? `<div class="stack">${rule.source_tables.map(table => `<div class="card" style="box-shadow:none">
        <strong>${escapeHtml(table.name || table.key || "未命名表格")}</strong>
        <p class="subtle">${escapeHtml(table.purpose || "")} · ${table.required === false ? "可选表" : "必需表"}</p>
        <div>${(table.columns || []).map(column => `<span class="badge info" style="margin:3px">${escapeHtml(column.name || column.key)}${column.required ? "（必填）" : ""}</span>`).join("")}</div>
      </div>`).join("")}</div>` : `<div class="empty">尚未从原文确认源表，不会自行编造。</div>`}
      <p class="subtle">稳定关联字段（人员或业务记录）：${(rule.stable_person_keys || []).map(item => escapeHtml(ruleText(item, ""))).filter(Boolean).join("、") || "尚未确认"}</p>
      <h3>团队或人员归属口径</h3>
      ${(rule.assignment_chains || []).length ? `<div class="stack">${rule.assignment_chains.map(chain => `<article class="card assignment-chain-card" style="box-shadow:none">
        <div class="card-head">
          <strong>${escapeHtml(chain.source_table)}：${escapeHtml(chain.mode_label)}</strong>
          <span class="badge ${chain.direct_field_fallback ? "warn" : "good"}">${chain.direct_field_fallback ? "读取底表分组字段" : "不使用底表团队列代替"}</span>
        </div>
        <div class="assignment-path">${(chain.path || []).map((item, index) => `${index ? `<span aria-hidden="true">→</span>` : ""}<strong>${escapeHtml(item)}</strong>`).join("")}</div>
        ${(chain.steps || []).some(step => step.matching_labels?.length) ? `<div class="stack compact-stack" style="margin-top:10px">${chain.steps.map(step => step.matching_labels?.length ? `<p class="subtle"><strong>${escapeHtml(step.name)}：</strong>${step.matching_labels.map(escapeHtml).join(" → ")}</p>` : "").join("")}</div>` : ""}
        <p class="subtle">${escapeHtml(chain.unmatched_policy || "")}</p>
      </article>`).join("")}</div>` : `<div class="empty compact-empty">尚未形成可核对的归属链。若 Skill 规定了“业务字段→负责人→团队”，必须先形成固定映射链，不能直接使用底表团队列。</div>`}
      <h3>技能内固定映射</h3>
      ${(rule.fixed_lookup_catalog || []).length ? `<div class="grid">${rule.fixed_lookup_catalog.map(item => `<article class="card span-6" style="box-shadow:none">
        <strong>${escapeHtml(item.name)}</strong>
        <p>${escapeHtml(item.kind_label)} · ${item.item_count} 条</p>
        <p class="subtle">内容哈希 ${escapeHtml((item.content_hash || "").slice(0, 16))}…</p>
      </article>`).join("")}</div>
      <div class="notice">这些内容只按固定字典或清单读取，从未执行 Skill 中的脚本。若 Skill 写明了匹配顺序，系统会按页面列出的固定步骤处理；只有结果唯一时才归属，多个候选或未匹配记录都会进入待处理。</div>
      ${(rule.fixed_lookup_details || []).map(detail => `<details class="lookup-detail" style="margin-top:10px">
        <summary>${escapeHtml(detail.name)} · ${detail.entry_count} 条</summary>
        <p class="subtle">${escapeHtml(detail.source_table)}${detail.split_first ? ` · 遇到“${escapeHtml(detail.split_first)}”时取第一个值` : ""}${detail.normalization_labels?.length ? ` · ${detail.normalization_labels.map(escapeHtml).join("、")}` : ""}${detail.matching_labels?.length ? ` · 顺序：${detail.matching_labels.map(escapeHtml).join(" → ")}` : ""}</p>
        <div class="table-wrap mapping-table-wrap"><table><thead><tr><th>原始值</th><th>对应值</th></tr></thead><tbody>
          ${(detail.entries || []).map(entry => `<tr><td>${escapeHtml(entry.source)}</td><td>${escapeHtml(entry.target)}</td></tr>`).join("")}
        </tbody></table></div>
      </details>`).join("")}` :
      `<div class="empty compact-empty">技能中没有识别到可安全读取的固定映射；如计算依赖映射表，请将映射表一并上传或在修改说明中补充。</div>`}
      <h3>适用团队</h3>
      ${(rule.applicability_scopes || []).length ? `<div class="grid">${rule.applicability_scopes.map(scope => `<article class="card span-6" style="box-shadow:none">
        <strong>${escapeHtml(ruleValue(scope, ["scope_name", "team_name", "name", "scope"], "适用范围待确认"))}</strong>
        <p class="subtle">稳定团队标识：${escapeHtml(ruleValue(scope, ["stable_team_key", "team_key", "scope_key"], "尚未确认"))}</p>
        <p>${escapeHtml(ruleValue(scope, ["description", "purpose", "notes"], "该范围的具体口径仍以原文证据为准。"))}</p>
      </article>`).join("")}</div>` : `<div class="empty">尚未确认团队适用范围；不会把一套指标默认套给全部团队。</div>`}
      <h3>团队指标目录与目标</h3>
      ${(rule.metric_catalog || []).length ? `<div class="table-wrap"><table><thead><tr><th>指标</th><th>适用范围</th><th>单位</th><th>说明</th></tr></thead><tbody>
        ${rule.metric_catalog.map(item => `<tr>
          <td>${escapeHtml(businessMetricName(ruleValue(item, ["metric_name", "name"], "")))}</td>
          <td>${escapeHtml(ruleValue(item, ["scope_names", "scope_keys", "team_names", "team_name"], "待确认"))}</td>
          <td>${escapeHtml(ruleValue(item, ["unit"], "待确认"))}</td>
          <td>${escapeHtml(ruleValue(item, ["description", "purpose", "notes"], "—"))}</td>
        </tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">尚未从原文形成团队指标目录。</div>`}
      ${(rule.target_versions || []).length ? `<div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>指标</th><th>团队</th><th>目标</th><th>生效周期</th></tr></thead><tbody>
        ${rule.target_versions.map(item => `<tr>
          <td>${escapeHtml(businessMetricName(ruleValue(item, ["metric_name", "metric_key", "name"], "")))}</td>
          <td>${escapeHtml(ruleValue(item, ["scope_name", "scope_key", "team_name"], "待确认"))}</td>
          <td>${escapeHtml({at_least:"不低于",at_most:"不高于",equal:"等于"}[item.comparison] || "方向待确认")} ${escapeHtml(ruleValue(item, ["target_value", "value"], "待确认"))} ${escapeHtml(ruleValue(item, ["unit"], ""))}</td>
          <td>${escapeHtml(ruleValue(item, ["effective_period", "period", "applies_from"], "待确认"))}</td>
        </tr>`).join("")}
      </tbody></table></div>` : ""}
      <h3>随包参考文档</h3>
      ${(rule.reference_documents || []).length ? `<div class="stack">${rule.reference_documents.map(item => `<div class="card" style="box-shadow:none">
        <strong>${escapeHtml(item.file_name || "参考文档")}</strong>
        <p class="subtle">${item.paragraph_count ? `已安全读取 ${item.paragraph_count} 个文字段落` : "已随规则包保存"}${item.file_hash ? ` · 文件哈希 ${escapeHtml(item.file_hash.slice(0, 12))}…` : ""}</p>
        <p>参考资料不能单独补造公式；如与 SKILL.md 冲突，系统会阻止启用并要求人工确认。</p>
      </div>`).join("")}</div>` : `<div class="empty">本次技能包没有 docx 参考文档。</div>`}
      <h3>公式解释</h3>
      ${rule.formulas?.length ? `<div class="table-wrap"><table><thead><tr><th>结果项</th><th>计算范围</th><th>确定性公式</th><th>Skill对应</th><th>单位</th><th>取整</th><th>除数为0</th></tr></thead><tbody>
        ${rule.formulas.map(item => `<tr><td>${escapeHtml(item.result_name)}</td><td>${escapeHtml(item.subject_scope || "团队与整体")}</td><td>${escapeHtml(item.explanation)}</td><td>${escapeHtml(item.source_reference || "按原文人工核对")}</td><td>${escapeHtml(item.unit || "—")}</td><td>${escapeHtml(item.rounding)}${item.decimal_places === null || item.decimal_places === undefined ? "" : `，保留 ${item.decimal_places} 位`}</td><td>${escapeHtml(item.zero_policy || "不适用")}</td></tr>`).join("")}
      </tbody></table></div>` : rule.rule_explanations?.length ? `<div class="stack">
        ${rule.rule_explanations.map(item => `<article class="card" style="box-shadow:none"><strong>${escapeHtml(item.name)}</strong><p>${escapeHtml(item.explanation)}</p></article>`).join("")}
        <div class="notice">以上是从 SKILL.md 整理出的业务口径，还没有转换成经过校验的固定计算程序，因此试算保持关闭。</div>
      </div>` : `<div class="empty">尚未形成经过校验的确定性公式，试算保持关闭。</div>`}
      <h3>Skill 代码公式核对</h3>
      ${(rule.formula_audits || []).length ? `<div class="table-wrap"><table><thead><tr><th>结果项</th><th>核对状态</th><th>Skill位置</th><th>说明</th></tr></thead><tbody>
        ${rule.formula_audits.map(item => `<tr>
          <td>${escapeHtml(item.result_name || item.result_key || "结果")}</td>
          <td><span class="badge ${item.status === "passed" ? "good" : item.status === "failed" ? "bad" : "warn"}">${escapeHtml(item.status_label || "需人工核对")}</span></td>
          <td>${item.source_line ? `SKILL.md 第 ${escapeHtml(item.source_line)} 行` : "未找到可静态核对公式"}</td>
          <td>${escapeHtml(item.message || "")}</td>
        </tr>`).join("")}
      </tbody></table></div>` : `<div class="empty compact-empty">Skill 没有可安全静态读取的代码公式；请按原文证据和底表验证结果人工核对。</div>`}
      <h3 style="margin-top:20px">仍需确认</h3>
      ${(rule.unresolved || []).length ? `<ul>${rule.unresolved.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<p class="success-text">结构化草稿没有未解决项；仍须发布管理员核对原文后确认。</p>`}
      <h3>规则与参考资料原文证据</h3>
      ${(rule.evidence || []).length ? `<div class="stack">${rule.evidence.map(item => `<blockquote class="card" style="box-shadow:none;margin:0">
        <strong>${escapeHtml(item.topic || "规则依据")}</strong>
        <p class="subtle">${escapeHtml(item.path || rule.skill_path)} · 第 ${item.line_start}–${item.line_end} ${item.location_kind === "paragraph" ? "段" : "行"}</p>
        <p>${escapeHtml(item.excerpt || "")}</p>
      </blockquote>`).join("")}</div>` : `<div class="empty">尚未生成原文证据。</div>`}
      <div class="divider"></div>
      <p class="subtle">规则文件哈希：${escapeHtml(rule.file_hash)}<br/>结构化规则哈希：${escapeHtml(rule.rule_spec_hash || "尚未生成")}</p>`;
    openDrawer();
    await bindRuleWorkspace(rule);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function showRulePreview(ruleRef, result) {
  const counts = result.achievement_counts || {};
  drawerContent.innerHTML = `
    <p class="eyebrow">规则底表验证</p>
    <h2>指标完成情况</h2>
    <p><span class="badge ${statusClass(result.status)}">${escapeHtml(result.status_label)}</span></p>
    <div class="notice">${escapeHtml(result.notice)}</div>
    <div class="metric-row preview-metrics">
      <div class="metric"><span>统计对象</span><strong>${result.participant_count}</strong></div>
      <div class="metric"><span>计算成功</span><strong>${result.success_count}</strong></div>
      <div class="metric"><span>达到目标</span><strong>${counts.achieved || 0}</strong></div>
      <div class="metric"><span>未达到目标</span><strong>${counts.not_achieved || 0}</strong></div>
      <div class="metric"><span>目标待确认</span><strong>${counts.unknown || 0}</strong></div>
    </div>
    <p class="subtle">验证编号 ${escapeHtml(result.preview_no)} · 规则 ${escapeHtml(result.rule_version)} · 草稿第 ${result.draft_revision} 版</p>
    <div class="toolbar">
      <button class="button" id="rule-preview-back" type="button">返回规则工作台</button>
    </div>
    <div class="divider"></div>
    <h3>使用的底表</h3>
    <div class="stack compact-stack">
      ${(result.source_versions || []).map(item => `<div class="card" style="box-shadow:none">
        <strong>${escapeHtml(item.table_name)}</strong>
        <p class="subtle">第 ${item.version} 版 · ${escapeHtml(item.file_name)} · ${item.published ? "正式数据版本" : "仅用于规则验证"}</p>
      </div>`).join("")}
    </div>
    <h3 style="margin-top:20px">逐项结果</h3>
    ${(result.results || []).length ? `<div class="stack">
      ${result.results.map(item => `<article class="card preview-result-card" style="box-shadow:none">
        <div class="card-head"><div><strong>${escapeHtml(item.person_name || "未匹配统计对象")}</strong></div>
          ${item.errors?.length ? `<span class="badge bad">计算异常</span>` : `<span class="badge good">计算完成</span>`}
        </div>
        ${item.errors?.length ? item.errors.map(error => `<div class="notice error-text">${escapeHtml(error.message || "计算异常")}</div>`).join("") : `
          <div class="metric-result-grid">
            ${(item.value_items || []).map(metric => `<div class="metric-result">
              <div class="card-head"><strong>${escapeHtml(metric.name)}</strong><span class="badge ${statusClass(metric.status)}">${escapeHtml(metric.status_label)}</span></div>
              <div class="metric-result-value">${escapeHtml(metricDisplayText(metric))}</div>
              <p class="subtle">${metric.target_value
                ? `目标：${escapeHtml(metric.comparison_label || "")}${escapeHtml(metric.target_value)}${escapeHtml(metric.target_unit || "")}`
                : "尚未配置可比较的目标"}</p>
            </div>`).join("")}
          </div>`}
        ${(item.lineage_summary || []).length ? `<details style="margin-top:12px">
          <summary>查看计算依据</summary>
          <div class="stack compact-stack" style="margin-top:8px">
            ${item.lineage_summary.map(entry => `<div>
              <strong>${escapeHtml(entry.result_name)}</strong>
              <p class="subtle">使用字段：${entry.fields?.length ? entry.fields.map(escapeHtml).join("、") : "固定规则值"}<br/>
              ${entry.metrics?.length ? `基础指标：${escapeHtml(lineageMetricText(entry))}<br/>` : ""}
              来源：${escapeHtml(lineageSourceText(entry))}</p>
            </div>`).join("")}
          </div>
        </details>` : ""}
      </article>`).join("")}
    </div>` : `<div class="empty compact-empty">本次没有可展示的指标结果。</div>`}
    <div class="notice" style="margin-top:18px">输入哈希 ${escapeHtml(result.input_hash.slice(0, 16))}… · 输出哈希 ${escapeHtml(result.output_hash.slice(0, 16))}…；相同底表、相同固定规则版本会得到相同结果。</div>`;
  openDrawer();
  $("#rule-preview-back")?.addEventListener("click", () => showRuleDetail(ruleRef));
}

function renderPeriodForm() {
  const slot = $("#period-form-slot");
  slot.innerHTML = `
    <article class="card" style="margin-top:16px">
      <div class="card-head"><div><h3>新建考核周期</h3><p>周期创建后不会自动生成任何人员或成绩。</p></div></div>
      <form id="period-form" class="form-grid">
        <label>周期名称<input name="label" placeholder="例如：2026年7月" required /></label>
        <label>周期类型<select name="period_type"><option value="month">月度</option><option value="quarter">季度</option><option value="half_year">半年度</option><option value="year">年度</option><option value="custom">自定义</option></select></label>
        <label>开始日期<input name="starts_on" type="date" required /></label>
        <label>结束日期<input name="ends_on" type="date" required /></label>
        <div class="form-actions span-12"><button class="button" id="cancel-period" type="button">取消</button><button class="button primary" type="submit">创建周期</button></div>
      </form>
    </article>`;
  $("#cancel-period").addEventListener("click", () => { slot.innerHTML = ""; });
  $("#period-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const body = Object.fromEntries(form.entries());
    const result = await mutate(event.submitter, () => api("periods", { method: "POST", body: JSON.stringify(body) }), "考核周期已创建");
    state.selectedPeriod = result.period_ref;
  });
  slot.scrollIntoView({ behavior: "smooth" });
}

async function showCalculation(result) {
  const subjectName = result.result_subject_name || "人员";
  drawerContent.innerHTML = `
    <p class="eyebrow">绩效试算</p><h2>${escapeHtml(result.calculation_no)}</h2>
    <p><span class="badge ${statusClass(result.status)}">${escapeHtml(result.status_label)}</span></p>
    <div class="metric-row" style="grid-template-columns:repeat(3,1fr)">
      <div class="metric"><span>参与${escapeHtml(subjectName)}数</span><strong>${result.participant_count}</strong></div>
      <div class="metric"><span>成功${escapeHtml(subjectName)}数</span><strong>${result.success_count}</strong></div>
      <div class="metric"><span>异常${escapeHtml(subjectName)}数</span><strong>${result.error_count}</strong></div>
    </div>
    <p class="subtle">规则版本 ${escapeHtml(result.rule_version)} · 计算人 ${escapeHtml(result.calculated_by)} · ${formatTime(result.calculated_at)}</p>
    ${result.error_count ? `<div class="notice error-text">仍有 ${result.error_count} 个${escapeHtml(subjectName)}计算异常，必须修正源数据并重新试算后才能发布正式结果。</div>` : ""}
    <div class="toolbar">
      <button class="button" id="export-calculation">下载结果 Excel</button>
      ${result.error_count ? `<button class="button" id="export-calculation-errors">下载异常明细</button>` : ""}
      ${state.shell.identity.capabilities.publish && result.status === "trial" && result.error_count === 0 ? `<button class="button primary" id="publish-calculation">确认发布正式结果</button>` : ""}
      ${state.shell.identity.capabilities.performance_upload && result.status === "trial" ? `<button class="button danger" id="abandon-calculation">放弃试算</button>` : ""}
    </div>
    <div class="divider"></div>
    ${result.results.length ? `<div class="table-wrap"><table><thead><tr><th>${escapeHtml(subjectName)}</th><th>稳定标识</th><th>计算结果</th><th>来源</th></tr></thead><tbody>
      ${result.results.map(item => `<tr><td>${escapeHtml(item.person_name)}</td><td>${escapeHtml(item.person_key)}</td>
        <td>${item.errors?.length
          ? item.errors.map(error => `<div class="error-text">${escapeHtml(error.message)}</div>`).join("")
          : item.value_items.map(value => `<div class="calculation-value-line">
              <strong>${escapeHtml(value.name)}</strong>：${escapeHtml(metricDisplayText(value))}
              <span class="badge ${statusClass(value.status)}">${escapeHtml(value.status_label || "目标待确认")}</span>
              ${value.target_value ? `<span class="subtle">目标 ${escapeHtml(value.comparison_label || "")}${escapeHtml(value.target_value)}${escapeHtml(value.target_unit || "")}</span>` : ""}
            </div>`).join("")}</td>
        <td><button class="button lineage-button" data-person="${escapeHtml(item.person_key)}">查看数据来源</button></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">本次没有可计算的${escapeHtml(subjectName)}</div>`}`;
  openDrawer();
  $("#export-calculation")?.addEventListener("click", event => download(`calculations/${result.calculation_ref}/export`, `绩效结果-${result.calculation_no}.xlsx`, event.currentTarget));
  $("#export-calculation-errors")?.addEventListener("click", event => download(`calculations/${result.calculation_ref}/errors.xlsx`, `绩效异常-${result.calculation_no}.xlsx`, event.currentTarget));
  $("#publish-calculation")?.addEventListener("click", async event => {
    if (!confirm("确认发布为正式绩效结果？发布人、规则版本、源表版本和原始文件会永久留痕。")) return;
    await mutate(event.currentTarget, () => api(`calculations/${result.calculation_ref}/publish`, { method: "POST" }), "正式绩效结果已发布");
    closeDrawer();
  });
  $("#abandon-calculation")?.addEventListener("click", async event => {
    if (!confirm("确认放弃本次试算？源表不会被删除。")) return;
    await mutate(event.currentTarget, () => api(`calculations/${result.calculation_ref}/abandon`, { method: "POST" }), "试算已放弃");
    closeDrawer();
  });
  document.querySelectorAll(".lineage-button").forEach(button => button.addEventListener("click", () => {
    const item = result.results.find(candidate => candidate.person_key === button.dataset.person);
    showLineage(result, item);
  }));
}

function showLineage(calculation, item) {
  if (!item) return;
  drawerContent.innerHTML = `
    <p class="eyebrow">数据血缘</p>
    <h2>${escapeHtml(item.person_name)}</h2>
    <p class="subtle">计算批次 ${escapeHtml(calculation.calculation_no)} · 规则版本 ${escapeHtml(calculation.rule_version)}</p>
    ${(item.lineage_summary || []).length ? `<div class="stack">${item.lineage_summary.map(entry => `
      <article class="card" style="box-shadow:none">
        <h3>${escapeHtml(entry.result_name)}</h3>
        <p class="subtle">使用规则版本：${escapeHtml(entry.rule_version)}</p>
        <p><strong>使用字段：</strong>${entry.fields?.length ? entry.fields.map(escapeHtml).join("、") : "固定规则值"}</p>
        ${entry.metrics?.length ? `<p><strong>基础指标：</strong>${escapeHtml(lineageMetricText(entry))}</p>` : ""}
        <p><strong>来源记录：</strong>${escapeHtml(lineageSourceText(entry))}</p>
      </article>`).join("")}</div>` : `<div class="empty">当前没有可展示的数据来源。</div>`}
    <div class="toolbar" style="margin-top:18px"><button class="button" id="lineage-back">返回试算结果</button></div>`;
  $("#lineage-back").addEventListener("click", () => showCalculation(calculation));
}

async function loadCaseImport(type) {
  state.batches = await api(`batches?business_type=${type === "case-master" ? "case_master" : "case_progress"}&limit=20`);
  const isMaster = type === "case-master";
  const caps = state.shell.identity.capabilities;
  content.innerHTML = `
    <div class="grid">
      <article class="card span-7">
        <div class="card-head"><div><h2>${isMaster ? "上传案件主表" : "上传案件进展表"}</h2>
          <p>${isMaster ? "原告案件底表、被告案件底表可以保持原样上传，仪表板和未入库的业务列会原样留痕，但不会误写正式案件字段。" : "案件进展与主表分开上传；先匹配案件，再生成时间线，不会按模糊名称猜测。"}</p></div></div>
        <div class="upload-panel" style="margin-top:18px">
          <form id="case-upload-form">
            ${isMaster ? `
              <input name="source_system" type="hidden" value="ERP" />
              <label>表格类型
                <select name="profile_key">
                  <option value="auto">自动识别（推荐）</option>
                  <option value="erp_plaintiff_case_master_v1">原告案件底表</option>
                  <option value="erp_defendant_case_master_v1">被告案件底表</option>
                  <option value="legal_ops_standard_case_master_v1">标准案件接口表</option>
                </select>
              </label>
              <label>导入方式<select name="import_mode"><option value="incremental">增量更新</option><option value="full">全量快照</option></select></label>
            ` : `
              <label>表格类型
                <select name="profile_key">
                  <option value="auto">自动识别原告/被告大表（推荐）</option>
                  <option value="erp_plaintiff_progress_snapshot_v1">原告案件大表</option>
                  <option value="erp_defendant_progress_snapshot_v1">被告案件大表</option>
                  <option value="legal_ops_standard_case_progress_v1">标准案件进展接口表</option>
                </select>
              </label>
              <label>本次进展快照日期<input name="snapshot_date" type="date" value="${new Date().toISOString().slice(0,10)}" required /></label>
              <label>本次文件录入人<input name="reporter_id" placeholder="员工编号或已关联账号" required /></label>
              <label>标准接口表的数据源（仅标准表使用）
                <select name="source_system">
                  <option value="ERP_PLAINTIFF_CASES">原告案件</option>
                  <option value="ERP_DEFENDANT_CASES">被告案件</option>
                  <option value="ERP">其他 ERP 案件</option>
                </select>
              </label>
              <input name="import_mode" type="hidden" value="append_or_update" />
            `}
            <label>选择文件<input name="file" type="file" accept=".xlsx,.csv" required ${caps.case_upload ? "" : "disabled"} /></label>
            <button class="button primary" type="submit" ${caps.case_upload ? "" : "disabled"}>上传并预览（自动识别）</button>
          </form>
        </div>
      </article>
      <article class="card span-5">
        <h3>${isMaster ? "已经适配的正式表" : "案件进展的边界"}</h3>
        <ul class="subtle">
          ${isMaster ? `<li>原告案件底表：识别“诉讼仲裁编号”</li><li>被告案件底表：识别“案件编号”</li><li>两类全量文件相互隔离，不会把另一类案件标成缺失</li><li>不覆盖下一步计划、内部备注、自定义标签和人工进展</li>` :
            `<li>原告进展与被告进展使用不同案件库</li><li>无法唯一匹配的记录进入错误队列</li><li>外部进展更新保留历史版本</li><li>未知程序节点不会由模型猜测</li><li>文件进展不会覆盖人工记录</li>`}
        </ul>
        <button class="button template-case" data-template="${isMaster ? "case_master" : "case_progress"}">${isMaster ? "下载标准接口表（仅新数据源使用）" : "下载当前进展接口表"}</button>
        ${isMaster ? `<p class="subtle" style="margin-top:12px">不需要把现有大表改成这个模板。标准接口表只给未来新增、尚未适配的数据源使用。</p>` :
          `<p class="subtle" style="margin-top:12px">可直接上传现有原告/被告案件大表。系统只提取其中已经填写的进展和计划列；空白进展不会生成记录，没有唯一匹配依据的行进入人工处理。</p>`}
      </article>
    </div>
    <div class="section-head" style="margin-top:28px"><div><h2>最近批次</h2><p>点击批次可查看逐行预览和错误原因。</p></div></div>
    ${renderBatchTable(state.batches)}`;
  $("#case-upload-form").addEventListener("submit", async event => {
    event.preventDefault();
    const result = await mutate(event.submitter, () => api(`${type}/upload`, { method: "POST", body: new FormData(event.currentTarget) }), "文件已识别并生成预览", false);
    state.lastPreviewBatch = result.batch_no;
    await showBatch(result.batch_no);
    await loadPage();
  });
  $(".template-case").addEventListener("click", event => download(`templates/${event.currentTarget.dataset.template}`, `${isMaster ? "案件主表" : "案件进展"}模板.xlsx`, event.currentTarget));
  bindBatchButtons();
}

function renderBatchTable(batches) {
  if (!batches.length) return `<div class="empty"><div><strong>还没有上传批次</strong>下载模板并上传真实业务文件后，这里会显示预览；系统不会自动生成演示数据。</div></div>`;
  return `<div class="table-wrap"><table><thead><tr><th>批次编号</th><th>文件</th><th>上传信息</th><th>结果</th><th>状态</th><th>操作</th></tr></thead><tbody>
    ${batches.map(item => `<tr>
      <td><strong>${escapeHtml(item.batch_no)}</strong><div class="subtle">${escapeHtml(item.business_type_label)}</div></td>
      <td>${escapeHtml(item.file_name)}
        ${item.source_profile?.label ? `<div class="subtle">${escapeHtml(item.source_profile.label)} · ${escapeHtml(item.source_profile.sheet || "CSV")}</div>` : ""}
        <div class="subtle">哈希 ${escapeHtml(item.file_hash.slice(0, 10))}…</div></td>
      <td>${escapeHtml(item.uploaded_by)}<div class="subtle">${formatTime(item.uploaded_at)}</div></td>
      <td>${Object.entries(item.counts).map(([key,value]) => `${escapeHtml(key)} ${value}`).join(" · ")}</td>
      <td><span class="badge ${statusClass(item.status)}">${escapeHtml(item.status_label)}</span></td>
      <td><div class="cell-actions"><button class="button batch-detail" data-batch="${escapeHtml(item.batch_no)}">查看预览</button>
        ${item.can_publish && state.shell.identity.capabilities.publish ? `<button class="button primary batch-publish" data-batch="${escapeHtml(item.batch_no)}" data-type="${escapeHtml(item.business_type)}" data-warnings="${Number(item.counts?.提醒 || 0)}">确认发布</button>` : ""}
        ${canAbandonBatch(item) ? `<button class="button danger batch-abandon" data-batch="${escapeHtml(item.batch_no)}">放弃</button>` : ""}
      </div></td></tr>`).join("")}
    </tbody></table></div>`;
}

function bindBatchButtons() {
  document.querySelectorAll(".batch-detail").forEach(button => button.addEventListener("click", () => showBatch(button.dataset.batch)));
  document.querySelectorAll(".batch-publish").forEach(button => button.addEventListener("click", async () => {
    const target = button.dataset.type === "case_master" ? "case-master" :
      button.dataset.type === "case_progress" ? "case-progress" : "performance/tables";
    const warnings = Number(button.dataset.warnings || 0);
    const message = warnings
      ? `该批次有 ${warnings} 条提醒，可能包含“仅按唯一姓名匹配”等需要人工核对的情况。确认你已查看提醒和原文，再发布正式数据？`
      : "确认将当前预览原子发布到正式数据？如果任何关键步骤失败，整个批次都会回滚。";
    if (!confirm(message)) return;
    await mutate(button, () => api(`${target}/${button.dataset.batch}/publish`, { method: "POST" }), "批次已原子发布");
  }));
  document.querySelectorAll(".batch-abandon").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("确认放弃这个尚未发布的批次？已发布版本不会受影响。")) return;
    await mutate(button, () => api(`batches/${button.dataset.batch}/abandon`, { method: "POST" }), "批次已放弃");
  }));
}

async function showValidatedSourceData(batchNo, offset = 0, rowStatus = "") {
  try {
    const filter = rowStatus ? `&row_status=${encodeURIComponent(rowStatus)}` : "";
    const item = await api(
      `batches/${encodeURIComponent(batchNo)}?offset=${Math.max(0, offset)}&limit=100${filter}`,
    );
    const page = item.row_page || {
      offset: 0,
      limit: 100,
      total: item.rows.length,
      has_more: false,
    };
    const columns = [];
    for (const row of item.rows || []) {
      for (const key of Object.keys(row.data || {})) {
        if (!columns.includes(key)) columns.push(key);
        if (columns.length >= 10) break;
      }
      if (columns.length >= 10) break;
    }
    drawerContent.innerHTML = `
      <p class="eyebrow">底表数据</p>
      <h2>${escapeHtml(item.file_name)}</h2>
      <div class="source-data-drawer-summary">
        <span class="badge ${statusClass(item.status)}">${escapeHtml(item.status_label)}</span>
        <strong>${rowStatus === "error"
          ? `${page.total} 行问题`
          : rowStatus === "warning"
            ? `${page.total} 行提醒`
            : `共 ${page.total} 行`}</strong>
        <span>${Number(item.counts?.失败 || 0)
          ? `原表中有 ${item.counts.失败} 行需要处理`
          : Number(item.counts?.提醒 || 0)
            ? `${item.counts.提醒} 行不影响本周期指标，已保留提醒`
            : "全部校验通过"}</span>
      </div>
      <p class="subtle">这里只展示系统实际读取和校验的数据，不会修改原文件，也不会自动发布。</p>
      <div class="source-result-actions">
        <label class="source-data-filter">查看
          <select id="source-data-filter">
            <option value="" ${rowStatus === "" ? "selected" : ""}>全部数据</option>
            <option value="error" ${rowStatus === "error" ? "selected" : ""}>只看问题</option>
            <option value="warning" ${rowStatus === "warning" ? "selected" : ""}>只看提醒</option>
            <option value="valid" ${rowStatus === "valid" ? "selected" : ""}>只看通过</option>
          </select>
        </label>
        ${Number(item.counts?.失败 || 0)
          ? `<button class="button" id="download-source-data-errors">下载问题数据</button>`
          : ""}
      </div>
      ${(item.rows || []).length ? `<div class="source-data-table-wrap drawer-data-table"><table>
        <thead><tr><th>原表行号</th><th>结果</th>${columns.map(column => `<th>${escapeHtml(column)}</th>`).join("")}<th>问题</th></tr></thead>
        <tbody>${item.rows.map(row => `<tr class="${row.status === "error" ? "has-error" : row.status === "warning" ? "has-warning" : ""}">
          <td>${row.source_row_number || "—"}</td>
          <td><span class="badge ${statusClass(row.status)}">${escapeHtml(row.status_label)}</span></td>
          ${columns.map(column => `<td>${escapeHtml(friendlyCellValue((row.data || {})[column]))}</td>`).join("")}
          <td>${row.errors?.length
            ? row.errors.map(error => `<div class="${error.severity === "错误" ? "error-text" : "warning-text"}">${escapeHtml(error.message)}</div>`).join("")
            : "通过"}</td>
        </tr>`).join("")}</tbody>
      </table></div>` : `<div class="plain-empty"><strong>没有可展示的数据</strong><p>请返回底表卡片查看具体校验提示。</p></div>`}
      <div class="toolbar source-data-pagination">
        <span class="subtle">当前显示第 ${page.offset + 1}–${Math.min(page.offset + item.rows.length, page.total)} 行</span>
        <div>
          <button class="button" id="source-data-prev" ${page.offset > 0 ? "" : "disabled"}>上一页</button>
          <button class="button" id="source-data-next" ${page.has_more ? "" : "disabled"}>下一页</button>
        </div>
      </div>`;
    openDrawer();
    $("#download-source-data-errors")?.addEventListener("click", event => {
      download(
        `batches/${encodeURIComponent(batchNo)}/errors.xlsx`,
        "底表问题数据.xlsx",
        event.currentTarget,
      );
    });
    $("#source-data-filter")?.addEventListener("change", event =>
      showValidatedSourceData(batchNo, 0, event.target.value));
    $("#source-data-prev")?.addEventListener("click", () =>
      showValidatedSourceData(batchNo, Math.max(0, page.offset - page.limit), rowStatus));
    $("#source-data-next")?.addEventListener("click", () =>
      showValidatedSourceData(batchNo, page.offset + page.limit, rowStatus));
  } catch (error) {
    showToast(error.message, true);
  }
}

async function showBatch(batchNo, offset = 0, rowStatus = "") {
  try {
    const query = `?offset=${Math.max(0, offset)}&limit=100${rowStatus ? `&row_status=${encodeURIComponent(rowStatus)}` : ""}`;
    const item = await api(`batches/${encodeURIComponent(batchNo)}${query}`);
    const page = item.row_page || { offset: 0, limit: 100, total: item.rows.length, has_more: false };
    const profile = item.source_profile || {};
    drawerContent.innerHTML = `
      <p class="eyebrow">${escapeHtml(item.business_type_label)}</p>
      <h2>${escapeHtml(item.batch_no)}</h2>
      <p><span class="badge ${statusClass(item.status)}">${escapeHtml(item.status_label)}</span></p>
      <p class="subtle">${escapeHtml(item.file_name)} · 上传人 ${escapeHtml(item.uploaded_by)} · ${formatTime(item.uploaded_at)}</p>
      ${profile.label ? `<div class="notice" style="margin:12px 0">
        <strong>已识别：${escapeHtml(profile.label)}</strong><br/>
        数据工作表：${escapeHtml(profile.sheet || "CSV")} · ${profile.column_count} 列
        ${profile.owner_link_policy ? `<br/>负责人处理：${escapeHtml(profile.owner_link_policy)}` : ""}
        ${profile.team_link_policy ? `<br/>团队处理：${escapeHtml(profile.team_link_policy)}` : ""}
        ${profile.field_mapping?.length ? `<details style="margin-top:8px"><summary>查看正式字段对应关系</summary>
          <div class="table-wrap" style="margin-top:8px"><table><thead><tr><th>系统业务字段</th><th>原表字段</th></tr></thead><tbody>
            ${profile.field_mapping.map(map => `<tr><td>${escapeHtml(map.business_field)}</td><td>${escapeHtml(map.source_header)}</td></tr>`).join("")}
          </tbody></table></div></details>` : ""}
      </div>` : ""}
      <div class="toolbar">
        <button class="button" id="download-batch-errors">下载错误数据</button>
        ${item.can_publish && state.shell.identity.capabilities.publish ? `<button class="button primary batch-publish" data-batch="${escapeHtml(item.batch_no)}" data-type="${escapeHtml(item.business_type)}" data-warnings="${Number(item.counts?.提醒 || 0)}">确认发布</button>` : ""}
        ${canAbandonBatch(item) ? `<button class="button danger batch-abandon" data-batch="${escapeHtml(item.batch_no)}">放弃批次</button>` : ""}
      </div><div class="divider"></div>
      <div class="toolbar" style="justify-content:space-between">
        <label>查看记录
          <select id="batch-row-status">
            <option value="" ${rowStatus === "" ? "selected" : ""}>全部</option>
            <option value="error" ${rowStatus === "error" ? "selected" : ""}>只看错误</option>
            <option value="warning" ${rowStatus === "warning" ? "selected" : ""}>只看提醒</option>
            <option value="valid" ${rowStatus === "valid" ? "selected" : ""}>只看可发布</option>
            <option value="skipped" ${rowStatus === "skipped" ? "selected" : ""}>只看无变化</option>
          </select>
        </label>
        <span class="subtle">共 ${page.total} 条，本页 ${item.rows.length} 条</span>
      </div>
      ${item.rows.length ? `<div class="table-wrap"><table><thead><tr><th>原始行</th><th>处理结果</th><th>数据预览</th><th>错误原因</th></tr></thead><tbody>
        ${item.rows.map(row => `<tr><td>${row.source_row_number || "来源文件缺失项"}</td>
          <td><span class="badge ${statusClass(row.status)}">${escapeHtml(row.action_label || row.status_label)}</span></td>
          <td>${Object.entries(row.data || {}).slice(0, 5).map(([key,value]) => `<div><span class="subtle">${escapeHtml(key)}</span> ${escapeHtml(value)}</div>`).join("") || "—"}</td>
          <td>${row.errors.length ? row.errors.map(error => `<div class="${error.severity === "错误" ? "error-text" : ""}">${escapeHtml(error.message)}</div>`).join("") : "校验通过"}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">当前筛选条件下没有记录</div>`}
      <div class="toolbar" style="justify-content:flex-end;margin-top:12px">
        <button class="button" id="batch-page-prev" ${page.offset > 0 ? "" : "disabled"}>上一页</button>
        <button class="button" id="batch-page-next" ${page.has_more ? "" : "disabled"}>下一页</button>
      </div>`;
    openDrawer();
    $("#download-batch-errors").addEventListener("click", event => download(`batches/${encodeURIComponent(batchNo)}/errors.xlsx`, `导入错误-${batchNo}.xlsx`, event.currentTarget));
    $("#batch-row-status")?.addEventListener("change", event => showBatch(batchNo, 0, event.target.value));
    $("#batch-page-prev")?.addEventListener("click", () => showBatch(batchNo, Math.max(0, page.offset - page.limit), rowStatus));
    $("#batch-page-next")?.addEventListener("click", () => showBatch(batchNo, page.offset + page.limit, rowStatus));
    bindBatchButtons();
  } catch (error) { showToast(error.message, true); }
}

async function loadBatches() {
  state.batches = await api("batches?limit=100");
  content.innerHTML = `
    <div class="section-head"><div><h2>全部导入批次</h2><p>相同文件重复上传会返回原批次，不会重复写入。</p></div>
      <label>业务类型<select id="batch-filter"><option value="">全部</option><option value="performance_rule_package">绩效规则包</option><option value="performance_source_table">绩效源表</option><option value="case_master">案件主表</option><option value="case_progress">案件进展</option></select></label>
    </div>${renderBatchTable(state.batches)}`;
  $("#batch-filter").addEventListener("change", async event => {
    state.batches = await api(`batches?limit=100${event.target.value ? `&business_type=${encodeURIComponent(event.target.value)}` : ""}`);
    content.querySelector(".table-wrap, .empty").outerHTML = renderBatchTable(state.batches);
    bindBatchButtons();
  });
  bindBatchButtons();
}

async function loadErrors() {
  state.errors = await api("errors?limit=100");
  const types = [
    ["", "全部错误"], ["case_not_found", "找不到案件"], ["multiple_case_candidates", "多个案件候选"],
    ["invalid_date", "日期错误"], ["duplicate_progress", "重复进展"], ["unknown_procedure_node", "未知程序节点"],
    ["missing_content", "缺少进展内容"], ["person_not_found", "人员无法匹配"], ["cross_table_missing", "跨表记录缺失"],
  ];
  content.innerHTML = `
    <div class="section-head"><div><h2>待处理错误</h2><p>错误未处理前，所在批次不能发布。</p></div>
      <label>错误类型<select id="error-filter">${types.map(([value,label]) => `<option value="${value}">${label}</option>`).join("")}</select></label>
    </div>${renderErrors(state.errors)}`;
  $("#error-filter").addEventListener("change", async event => {
    state.errors = await api(`errors?limit=100${event.target.value ? `&error_type=${encodeURIComponent(event.target.value)}` : ""}`);
    content.querySelector(".table-wrap, .empty").outerHTML = renderErrors(state.errors);
    bindErrorButtons();
  });
  bindErrorButtons();
}

function renderErrors(errors) {
  if (!errors.length) return `<div class="empty"><div><strong>当前没有待处理错误</strong>新上传文件的错误会集中显示在这里。</div></div>`;
  return `<div class="table-wrap"><table><thead><tr><th>批次</th><th>文件与行号</th><th>错误原因</th><th>数据预览</th><th>操作</th></tr></thead><tbody>
    ${errors.map(item => `<tr><td>${escapeHtml(item.batch_no)}<div class="subtle">${escapeHtml(item.business_type_label)}</div></td>
      <td>${escapeHtml(item.file_name)}<div class="subtle">原始第 ${item.source_row_number} 行</div></td>
      <td>${item.errors.map(error => `<div class="error-text">${escapeHtml(error.message)}</div>`).join("")}</td>
      <td>${Object.entries(item.data || {}).slice(0,4).map(([key,value]) => `<div><span class="subtle">${escapeHtml(key)}</span> ${escapeHtml(value)}</div>`).join("")}</td>
      <td><div class="cell-actions">
        ${item.can_resolve ? `<button class="button primary error-resolve" data-row="${escapeHtml(item.row_ref)}">人工处理</button>` : ""}
        <button class="button error-detail" data-batch="${escapeHtml(item.batch_no)}">查看批次</button>
        <button class="button error-download" data-batch="${escapeHtml(item.batch_no)}">下载错误</button>
      </div></td></tr>`).join("")}
    </tbody></table></div>`;
}

function bindErrorButtons() {
  document.querySelectorAll(".error-resolve").forEach(button => button.addEventListener("click", () => showErrorResolution(button.dataset.row)));
  document.querySelectorAll(".error-detail").forEach(button => button.addEventListener("click", () => showBatch(button.dataset.batch)));
  document.querySelectorAll(".error-download").forEach(button => button.addEventListener("click", () => download(`batches/${button.dataset.batch}/errors.xlsx`, `导入错误-${button.dataset.batch}.xlsx`, button)));
}

async function showErrorResolution(rowRef) {
  try {
    const options = await api(`errors/${encodeURIComponent(rowRef)}/options`);
    const current = options.current || {};
    drawerContent.innerHTML = `
      <p class="eyebrow">案件进展错误处理</p>
      <h2>原始第 ${options.source_row_number} 行</h2>
      <div class="notice">${escapeHtml(options.notice)}</div>
      <div class="stack compact-stack" style="margin:14px 0">
        ${(options.errors || []).map(error => `<div class="error-text">${escapeHtml(error.message || "校验失败")}</div>`).join("")}
      </div>
      <form id="error-resolution-form" class="stack">
        <label>明确选择案件（无法匹配或多个候选时必选）
          <select name="case_ref">
            <option value="">保留稳定案件ID自动匹配</option>
            ${(options.cases || []).map(item => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("")}
          </select>
        </label>
        <label>程序节点
          <select name="procedure_node">
            <option value="">保留原值或请选择</option>
            ${(options.procedure_nodes || []).map(value => `<option value="${escapeHtml(value)}" ${String(current.procedure_node || "") === String(value) ? "selected" : ""}>${escapeHtml(value)}</option>`).join("")}
          </select>
        </label>
        <label class="checkbox-line"><input name="apply_same_node" type="checkbox" checked /> 同一批次中相同原值一起改（适合大表）</label>
        <label>录入人员
          <select name="reporter_ref">
            <option value="">保留原值或请选择</option>
            ${(options.people || []).map(item => `<option value="${escapeHtml(item.value)}" ${String(current.reporter_id || "") === String(item.value) ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
          </select>
        </label>
        <label class="checkbox-line"><input name="apply_same_reporter" type="checkbox" checked /> 同一批次中相同录入人一起改</label>
        <label>进展日期<input name="progress_date" type="date" value="${escapeHtml(current.progress_date || "")}" /></label>
        <label>进展内容<textarea name="content" rows="7">${escapeHtml(current.content || "")}</textarea></label>
        <label>下一步计划<textarea name="next_plan" rows="4">${escapeHtml(current.next_plan || "")}</textarea></label>
        <div class="toolbar">
          <button class="button primary" type="submit">保存并重新校验整批</button>
          <button class="button" type="button" id="error-resolution-cancel">取消</button>
        </div>
      </form>`;
    openDrawer();
    $("#error-resolution-cancel").addEventListener("click", closeDrawer);
    $("#error-resolution-form").addEventListener("submit", async event => {
      event.preventDefault();
      const body = Object.fromEntries(new FormData(event.currentTarget).entries());
      body.apply_same_node = Boolean(body.apply_same_node);
      body.apply_same_reporter = Boolean(body.apply_same_reporter);
      const result = await mutate(event.submitter, () => api(`errors/${encodeURIComponent(rowRef)}/resolve`, {
        method: "POST",
        body: JSON.stringify(body),
      }), "已保存并重新校验", false);
      closeDrawer();
      await loadErrors();
      showToast(result.can_publish ? "本批次错误已处理，可由发布管理员确认发布" : `仍有 ${result.remaining_error_rows} 条错误待处理`);
    });
  } catch (error) {
    showToast(error.message, true);
  }
}

function openDrawer() {
  drawer.classList.remove("hidden"); drawerMask.classList.remove("hidden");
}
function closeDrawer() {
  drawer.classList.add("hidden"); drawerMask.classList.add("hidden");
}

loginForm.addEventListener("submit", async event => {
  event.preventDefault();
  state.token = $("#token-input").value.trim();
  sessionStorage.setItem("legalOpsCredential", state.token);
  loginError.textContent = "";
  await bootstrap();
});
$("#refresh-button").addEventListener("click", event => mutate(event.currentTarget, loadPage, "页面已刷新", false));
$("#drawer-close").addEventListener("click", closeDrawer);
drawerMask.addEventListener("click", closeDrawer);
document.addEventListener("keydown", event => { if (event.key === "Escape") closeDrawer(); });

bootstrap();
