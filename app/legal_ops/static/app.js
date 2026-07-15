let pages = [
  ["command", "作战指挥中心", "指"],
  ["work", "工作与日报", "工"],
  ["performance", "绩效与报告", "绩"],
  ["teams", "团队工作台", "组"],
  ["forest", "案件森林", "案"],
  ["travel", "出差协同", "行"],
];

const state = {
  token: sessionStorage.getItem("legalOpsCredential") || "",
  page: "command",
  shell: null,
  teamId: "",
  caseFilters: {},
  casePage: 1,
  bulkFollowupPreview: null,
};

const content = document.querySelector("#content");
const loginModal = document.querySelector("#login-modal");
const loginForm = document.querySelector("#login-form");
const loginError = document.querySelector("#login-error");
const demoLoginButton = document.querySelector("#demo-login-button");
const navigation = document.querySelector("#navigation");
const teamFilter = document.querySelector("#team-filter");
const drawer = document.querySelector("#drawer");
const drawerBackdrop = document.querySelector("#drawer-backdrop");
const drawerContent = document.querySelector("#drawer-content");

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

const formatBusinessTime = (value) => {
  if (!value) return "暂未记录";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(parsed);
};

async function api(path) {
  const response = await fetch(`/legal-ops/api/${path}`, {
    headers: { "X-Legal-Ops-Token": state.token },
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    sessionStorage.removeItem("legalOpsCredential");
    loginModal.classList.remove("hidden");
  }
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`);
  return payload;
}

async function apiMutation(path, method, body) {
  const response = await fetch(`/legal-ops/api/${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Legal-Ops-Token": state.token,
    },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    sessionStorage.removeItem("legalOpsCredential");
    loginModal.classList.remove("hidden");
  }
  if (!response.ok) {
    const detail = typeof payload.detail === "string"
      ? payload.detail
      : (payload.detail?.reason || payload.detail?.error_code || `请求失败 (${response.status})`);
    throw new Error(detail);
  }
  return payload;
}

async function downloadExport(period, fileFormat) {
  const response = await fetch(`/legal-ops/api/exports/${encodeURIComponent(period)}/${encodeURIComponent(fileFormat)}`, {
    headers: { "X-Legal-Ops-Token": state.token },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `导出失败 (${response.status})`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `legal-ops-${period}-report.${fileFormat}`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  toast(`${period === "weekly" ? "周报" : "月报"} ${fileFormat.toUpperCase()} 已生成`);
}

function badge(value) {
  return `<span class="badge ${escapeHtml(value)}">${escapeHtml(value)}</span>`;
}

function renderNavigation() {
  navigation.innerHTML = pages.map(([key, label, icon]) => `
    <button class="nav-button ${state.page === key ? "active" : ""}" data-page="${key}">
      <span class="nav-icon">${icon}</span><span>${label}</span>
    </button>`).join("");
}

function pageTitle() {
  return pages.find(([key]) => key === state.page)?.[1] || "法务运营中台";
}

async function bootstrap() {
  if (!state.token) return;
  try {
    state.shell = await api("shell");
    if (state.shell.mode === "sandbox_live") {
      const role_ids = state.shell.scope.role_ids || [];
      pages = [
        ["overview", "工作总览", "总"],
        ["reports", "报告中心", "报"],
        ["cases", "案件工作台", "案"],
        ["travel", "出差协同", "行"],
        ["team", "团队", "团"],
      ];
      if (role_ids.includes("tenant_admin") || role_ids.includes("system_admin")) pages.push(["audit", "审计证据", "审"]);
      if (!["overview", "reports", "cases", "travel", "team", "audit"].includes(state.page)) state.page = "overview";
      teamFilter.closest("label")?.classList.add("hidden");
      document.querySelector("#environment-subtitle").textContent = "运营决策 · Agent2 灰测";
      document.querySelector("#environment-title").textContent = "灰测真实数据";
      document.querySelector("#environment-detail").textContent = "服务器 PostgreSQL · 权限隔离";
      document.querySelector("#data-mode-title").textContent = "灰测真实数据";
    } else {
      document.querySelector("#environment-subtitle").textContent = "运营决策 · 演示环境";
      document.querySelector("#environment-title").textContent = "演示数据";
      document.querySelector("#environment-detail").textContent = "隔离 Fixture · 不代表真实业务";
      document.querySelector("#data-mode-title").textContent = "演示数据";
    }
    loginModal.classList.add("hidden");
    document.querySelector("#tenant-eyebrow").textContent = state.shell.tenant.name;
    document.querySelector("#fixture-banner span").textContent = state.shell.fixture_notice;
    document.querySelector("#identity-button").textContent = state.shell.mode === "sandbox_live" ? "我" : (state.shell.scope.user_id || "S").slice(0, 1).toUpperCase();
    document.querySelector("#identity-button").title = state.shell.mode === "sandbox_live" ? "退出或切换登录身份" : "当前身份";
    teamFilter.innerHTML = `<option value="">全部团队</option>${state.shell.teams.map(team =>
      `<option value="${escapeHtml(team.id)}">${escapeHtml(team.name)}</option>`).join("")}`;
    renderNavigation();
    await loadPage();
  } catch (error) {
    loginError.textContent = error.message;
  }
}

async function loadPage() {
  document.querySelector("#page-title").textContent = pageTitle();
  renderNavigation();
  content.innerHTML = `<div class="loading-card">正在加载 ${pageTitle()}…</div>`;
  try {
    if (state.shell?.mode === "sandbox_live") {
      if (state.page === "cases") return loadLiveCases();
      if (state.page === "reports") return renderLiveReports(await api("workspace/reports"));
      if (state.page === "team") return renderLiveTeam(await api("workspace/team"));
      if (state.page === "travel") return renderLiveTravel(await api("workspace/travel"));
      const live = await api("phase2");
      if (state.page === "audit") return renderLiveAudit(live);
      const cases = await api("workspace/cases?page=1&page_size=20");
      return renderProductOverview(live, cases);
    }
    if (state.page === "command") return renderOverview(await api("overview"));
    if (state.page === "work") return renderReport(await api("reports/daily"));
    if (state.page === "performance") return renderPerformanceHub(await api("performance"));
    if (state.page === "teams") return renderTeams(await api("teams"));
    if (state.page === "travel") return renderTravel(await api("travel"));
    if (state.page === "metrics") return renderMetrics(await api("metrics"));
    if (state.page === "forest") return loadCases();
    if (state.page === "admin") return renderAdminHub(await Promise.all([api("quality"), api("sources"), api("permissions")]));
    if (state.page === "agent2") return renderAgent2(await api("phase2"));
    if (state.page === "quality") return renderQuality(await api("quality"));
    if (state.page === "sources") return renderSources(await api("sources"));
    if (state.page === "permissions") return renderPermissions(await api("permissions"));
  } catch (error) {
    content.innerHTML = `<div class="empty-state"><strong>无法加载</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function loadLiveCases(page = 1) {
  const form = document.querySelector("#live-case-filters");
  const params = new URLSearchParams({ page: String(page), page_size: "20" });
  for (const [key, value] of Object.entries(state.caseFilters || {})) {
    if (value) params.set(key, value);
  }
  if (form) {
    const values = new FormData(form);
    for (const key of ["query", "case_type", "stage", "progress_status"]) {
      if (values.get(key)) params.set(key, values.get(key));
      else params.delete(key);
    }
  }
  renderLiveCaseWorkspace(await api("workspace/cases?" + params.toString()));
}

function renderProductOverview(live, cases) {
  const summary = cases.summary;
  const overviewMetrics = [
    ["团队共享案件", summary.total, "cases"],
    ["本人负责", summary.assigned_to_me, "cases"],
    ["团队协作", summary.shared_with_me, "cases"],
    ["原告案件", summary.plaintiff, "cases:plaintiff_case"],
    ["被告案件", summary.defendant, "cases:defendant_case"],
    ["周报 / 月报", live.summary.periodic_reports, "reports"],
    ["出差登记", live.summary.travel_intents, "travel"],
  ];
  if ((state.shell?.scope?.role_ids || []).some(role => ["tenant_admin", "system_admin"].includes(role))) overviewMetrics.push(["失败或阻断", live.summary.failures, "audit"]);
  content.innerHTML = `${sectionHeading("工作总览", "只展示当前登录人权限范围内的真实业务数据")}
    <div class="cards">${overviewMetrics.map(([label, value, route]) => `<article class="metric-card actionable-card"><span>${label}</span><strong>${value}<small> 条</small></strong><button data-overview-route="${route}">查看构成 →</button></article>`).join("")}</div>
    <div class="grid-2"><section class="panel"><h3 class="panel-title">案件阶段分布</h3>${Object.entries(summary.stage_distribution).map(([label, value]) => `<div class="bar-row"><span>${escapeHtml(label)}</span><div class="bar-track"><div class="bar" style="width:${Math.max(4, value / Math.max(summary.total, 1) * 100)}%"></div></div><strong>${value}</strong></div>`).join("") || '<div class="empty-state">暂无案件</div>'}</section>
    <section class="panel"><h3 class="panel-title">数据与权限说明</h3><p>案件、进展、报告、出差和通知均来自当前服务器 PostgreSQL。当前案件权限：${escapeHtml(summary.access_label)}；本人负责 ${summary.assigned_to_me} 件，团队协作 ${summary.shared_with_me} 件，可登记进展 ${summary.writable} 件。报告仍仅展示本人数据。</p><button class="text-button" data-page="cases">查看案件工作台 →</button></section></div>`;
}

function renderLiveCaseWorkspace(data) {
  const filters = data.filters || {};
  state.casePage = Number(data.pagination?.page || 1);
  state.caseFilters = { case_type: filters.case_type || "", stage: filters.stage || "", query: filters.query || "", progress_status: filters.progress_status || "" };
  content.innerHTML = `${sectionHeading("案件工作台", `当前为${escapeHtml(data.summary.access_label)}：负责人表示案件分配，团队协作案件也可按权限登记进展`)}
    <form id="live-case-filters" class="toolbar">
      <input name="query" value="${escapeHtml(filters.query || "")}" placeholder="案件名称、案号或当事人" />
      <select name="case_type"><option value="">全部案件</option><option value="plaintiff_case" ${filters.case_type === "plaintiff_case" ? "selected" : ""}>原告案件</option><option value="defendant_case" ${filters.case_type === "defendant_case" ? "selected" : ""}>被告案件</option></select>
      <select name="stage"><option value="">全部阶段</option>${[
        ["intended_filing", "拟诉"], ["litigation", "诉讼中"], ["enforcement", "执行中"],
        ["accepted", "受理"], ["hearing", "开庭"], ["adjudicated", "审结"],
        ["performance", "履行"], ["closed", "已结案"],
      ].map(([value, label]) => `<option value="${value}" ${filters.stage === value ? "selected" : ""}>${label}</option>`).join("")}</select>
      <select name="progress_status"><option value="">全部进展状态</option><option value="active" ${filters.progress_status === "active" ? "selected" : ""}>已有有效进展</option><option value="missing" ${filters.progress_status === "missing" ? "selected" : ""}>暂无有效进展</option></select>
      <button class="primary-button compact-primary" type="submit">筛选</button>
    </form>
    <div class="case-summary-strip"><span>共 ${data.pagination.total} 件</span><span>本人负责 ${data.summary.assigned_to_me} 件</span><span>团队协作 ${data.summary.shared_with_me} 件</span><span>可登记进展 ${data.summary.writable} 件</span><span>有进展 ${data.summary.with_progress} 件</span></div>
    <div class="product-case-grid">${data.items.map(item => `<article class="product-case-card"><div><span class="badge">${escapeHtml(item.case_type)}</span><span class="badge">${escapeHtml(item.stage)}</span><span class="badge">${escapeHtml(item.assignment)}</span></div><h3>${escapeHtml(item.case_name)}</h3><p>${escapeHtml(item.case_number)}</p><dl><div><dt>当前节点</dt><dd>${escapeHtml(item.node)}</dd></div><div><dt>负责人</dt><dd>${escapeHtml(item.owner_name)}</dd></div><div><dt>登记权限</dt><dd>${item.can_add_progress ? "可登记进展" : "仅查看"}</dd></div><div><dt>当事人</dt><dd>${escapeHtml(item.counterparties.join("、") || "暂未记录")}</dd></div>${renderBusinessFacts(item.business_facts)}<div><dt>追问策略</dt><dd>${escapeHtml(item.followup_policy)}</dd></div><div class="case-wide-field"><dt>最新进展</dt><dd>${escapeHtml(item.latest_progress)}</dd></div><div class="case-wide-field"><dt>下一步计划</dt><dd>${escapeHtml(item.next_plan)}</dd></div></dl><footer><span>${escapeHtml(item.source)} · 更新于 ${formatBusinessTime(item.updated_at)}</span><button class="text-button" data-phase2-case="${escapeHtml(item.case_ref)}">查看单案 →</button></footer></article>`).join("") || '<div class="empty-state">没有符合条件的案件</div>'}</div>
    <div class="pagination"><button ${data.pagination.page <= 1 ? "disabled" : ""} data-live-case-page="${data.pagination.page - 1}">上一页</button><span>第 ${data.pagination.page} / ${data.pagination.pages} 页</span><button ${data.pagination.page >= data.pagination.pages ? "disabled" : ""} data-live-case-page="${data.pagination.page + 1}">下一页</button></div>`;
  document.querySelector("#live-case-filters")?.addEventListener("submit", event => { event.preventDefault(); loadLiveCases(1); });
}

function renderLiveReports(data) {
  const summary = data.summary;
  content.innerHTML = `${sectionHeading("报告中心", "日报、周报、月报统一查看；每份报告完整展示用户可见正文")}
    <div class="cards">${[["全部报告", summary.total], ["日报", summary.daily], ["周报", summary.weekly], ["月报", summary.monthly]].map(([label, value]) => `<article class="metric-card"><span>${label}</span><strong>${value}<small> 份</small></strong></article>`).join("")}</div>
    <div class="report-product-list">${data.items.map((item, index) => `<details class="report-product-card" ${index === 0 ? "open" : ""}><summary><div><span class="badge">${escapeHtml(item.report_type)}</span><span class="badge">${escapeHtml(item.status)}</span><h3>${escapeHtml(item.owner_name)} · ${escapeHtml(item.period)}</h3><p>${escapeHtml(item.source)} · ${formatBusinessTime(item.updated_at)}</p></div><span>展开完整报告</span></summary><div class="report-sections">${Object.entries(item.sections).map(([section, values]) => renderReportSection(item, section, values)).join("")}</div>${renderReportActions(item)}</details>`).join("") || '<div class="empty-state">当前没有可见报告</div>'}</div>`;
}

const reportFieldLabels = {
  daily: { today_work: "今日工作", problems: "问题与风险", tomorrow_plan: "下一步计划" },
  weekly: { accomplishments: "本期完成", risks: "问题与风险", next_plan: "下一步计划", metrics: "关键指标" },
  monthly: { accomplishments: "本期完成", risks: "问题与风险", next_plan: "下一步计划", metrics: "关键指标" },
};

function reportFieldCode(item, visibleLabel) {
  return Object.entries(reportFieldLabels[item.report_type_code] || {}).find(([, label]) => label === visibleLabel)?.[0] || "";
}

function renderReportSection(item, section, values) {
  const fieldCode = reportFieldCode(item, section);
  const actionItems = item.actions?.items?.[fieldCode] || [];
  return `<section><h4>${escapeHtml(section)}</h4>${values.length ? `<ol>${values.map((value, index) => {
    const action = actionItems[index] || {};
    const controls = item.actions?.can_edit && action.item_ref
      ? `<span class="inline-actions"><button type="button" data-start-report-edit>修改</button><button type="button" class="danger-text" data-report-delete data-report-ref="${escapeHtml(item.report_ref)}" data-item-ref="${escapeHtml(action.item_ref)}" data-version="${item.actions.expected_version}">删除</button></span><form class="inline-edit-form hidden" data-report-edit-form data-report-ref="${escapeHtml(item.report_ref)}" data-item-ref="${escapeHtml(action.item_ref)}" data-version="${item.actions.expected_version}"><input name="value" maxlength="10000" required value="${escapeHtml(value)}" aria-label="修改报告条目" /><button class="primary-button" type="submit">保存修改</button><button type="button" data-cancel-inline-edit>取消</button></form>`
      : "";
    return `<li><span>${escapeHtml(value)}</span>${controls}</li>`;
  }).join("")}</ol>` : '<p class="muted">暂无内容</p>'}</section>`;
}

function renderReportActions(item) {
  if (!item.actions?.can_edit) return '<p class="muted report-readonly">该报告已提交或不在当前可编辑周期，仅供查看。</p>';
  const fields = reportFieldLabels[item.report_type_code] || {};
  return `<form class="report-action-form" data-report-append data-report-ref="${escapeHtml(item.report_ref)}" data-version="${item.actions.expected_version}"><label>新增到<select name="field_name">${Object.entries(fields).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></label><input name="value" maxlength="10000" required placeholder="输入要加入报告的原文" /><button class="primary-button compact-primary" type="submit">加入报告</button><button type="button" data-report-submit data-report-ref="${escapeHtml(item.report_ref)}" data-version="${item.actions.expected_version}">提交报告</button></form>`;
}

function renderLiveTeam(data) {
  content.innerHTML = `${sectionHeading(data.team_name, `${escapeHtml(data.identity_mapping.source)} · ${escapeHtml(data.identity_mapping.status)} · ${escapeHtml(data.identity_mapping.scope)}`)}
    <div class="cards">${[["成员", data.summary.members], ["已分配案件", data.summary.cases], ["进行中出差", data.summary.active_travel]].map(([label, value]) => `<article class="metric-card"><span>${label}</span><strong>${value}<small> ${label === "成员" ? "人" : "条"}</small></strong></article>`).join("")}</div>
    <div class="team-grid product-team-grid">${data.members.map(item => `<article class="team-card"><h3>${escapeHtml(item.name)}</h3><div class="mini-stats"><div><strong>${item.assigned_cases}</strong><span>分配案件</span></div><div><strong>${item.plaintiff_cases}</strong><span>原告</span></div><div><strong>${item.defendant_cases}</strong><span>被告</span></div><div><strong>${item.active_travel}</strong><span>出差</span></div></div><div class="team-members"><p>最近日报：${escapeHtml(item.latest_daily_date)} · ${escapeHtml(item.latest_daily_status)}</p><p>周报/月报：${item.periodic_reports} 份</p></div></article>`).join("") || '<div class="empty-state">暂无团队成员</div>'}</div>`;
}

function originLabel(value) {
  const labels = {
    real_user_message: "真实用户消息",
    server_acceptance_smoke: "服务器验收 Smoke",
    sandbox_fixture: "演示数据",
    system_generated: "系统生成",
    robot_followup: "机器人追踪",
    daily_report: "日报进展",
    human_record: "人工记录",
  };
  return `<span class="origin-chip ${escapeHtml(value)}">${escapeHtml(labels[value] || value || "来源未标记")}</span>`;
}

function auditCommandLabel(value) {
  const labels = {
    CreateCaseProgress: "新增案件进展",
    UpdateCaseProgress: "修改案件进展",
    DeleteCaseProgress: "删除案件进展",
    create_case_progress: "新增案件进展",
    update_case_progress: "修改案件进展",
    delete_case_progress: "删除案件进展",
    CreateTravelIntent: "登记出差",
    UpdateTravelIntent: "更新出差",
    UpdateCaseFollowupPolicy: "修改追问策略",
    TriggerCaseFollowupNow: "创建追问任务",
  };
  return labels[value] || "其他业务操作";
}

function auditResourceLabel(value) {
  const labels = {
    case_progress: "案件进展",
    case: "案件",
    travel_intent: "出差登记",
    collaboration_candidate: "协同候选",
    notification: "通知",
    report: "报告",
    case_followup_policy: "追问策略",
    case_followup_task: "追问任务",
  };
  return labels[value] || "其他业务对象";
}

function auditStatusLabel(value) {
  const labels = {
    executed: "已执行",
    duplicate: "重复请求（未重复写入）",
    blocked: "已阻断",
    failed: "失败",
    pending: "待处理",
  };
  return labels[value] || "状态异常";
}

function renderLiveOverview(data) {
  const summary = data.summary;
  content.innerHTML = `${sectionHeading("Agent2 灰测实时数据", "直接读取服务器数据库；验收数据、演示数据与真实用户消息分别标记")}
    <div class="live-mode-banner"><strong>服务器实时读模型</strong>${businessBadge(data.route_control?.route_mode || "未配置路由")}</div>
    <div class="cards">${[
      ["主体", summary.parties], ["案件", summary.cases], ["案件进展", summary.case_progress],
      ["出差登记", summary.travel_intents], ["协同候选", summary.collaboration_candidates],
      ["通知", summary.notifications], ["回执", summary.receipts], ["失败/阻断", summary.failures],
    ].map(([label, value]) => `<article class="metric-card"><span>${label}</span><strong>${value}<small> 条</small></strong></article>`).join("")}</div>
    <div class="grid-2 live-overview-grid"><section class="panel"><h3 class="panel-title">最新案件进展 <span>服务器数据库</span></h3>${data.case_progress.slice(0,6).map(item => `<article class="live-event"><strong>${escapeHtml(item.summary)}</strong><p>${escapeHtml(item.updated_at)}</p>${originLabel(item.data_origin)}</article>`).join("") || '<div class="empty-state">暂无进展</div>'}</section>
    <section class="panel"><h3 class="panel-title">通知传输状态 <span>排队、平台接受与确认送达分别展示</span></h3>${data.notifications.slice(0,6).map(item => `<article class="live-event"><strong>${escapeHtml(item.message_type)}</strong><p>${escapeHtml(item.status)}</p>${originLabel(item.data_origin)}</article>`).join("") || '<div class="empty-state">暂无通知</div>'}</section></div>`;
}

function renderLiveParties(data) {
  content.innerHTML = `${sectionHeading("原被告主体知识库", "按规范名称、别名或标识符查询 PostgreSQL 主体，不自动合并模糊候选")}
    <div class="toolbar"><input id="party-search" placeholder="企业名称、简称、统一社会信用代码" /><span class="badge">${data.items.length} 个主体</span></div>
    <div class="party-live-grid">${data.items.map(item => `<button class="party-live-card" data-live-party="${escapeHtml(item.party_id)}"><span>${escapeHtml(item.party_type)}</span><strong>${escapeHtml(item.canonical_name)}</strong><small>${escapeHtml(item.short_name || item.unified_social_credit_code || "无简称或标识符")}</small>${originLabel(item.data_origin)}</button>`).join("") || '<div class="empty-state">没有匹配主体</div>'}</div>`;
  document.querySelector("#party-search")?.addEventListener("change", async event => {
    renderLiveParties(await api(`phase2/parties?query=${encodeURIComponent(event.target.value)}`));
  });
}

async function showLiveParty(partyId) {
  const data = await api(`phase2/parties/${encodeURIComponent(partyId)}`);
  const party = data.party;
  openDrawer(`<div class="case-header"><p class="eyebrow">主体知识库</p><h2>${escapeHtml(party.canonical_name)}</h2><p>${escapeHtml(party.party_type)} · ${escapeHtml(party.data_quality)}</p>${originLabel(party.data_origin)}</div>
    <div class="grid-2"><section class="panel"><h3 class="panel-title">别名</h3>${data.aliases.map(item => `<p>${escapeHtml(item.alias)} ${originLabel(item.data_origin)}</p>`).join("") || '<p>无</p>'}</section><section class="panel"><h3 class="panel-title">标识符</h3>${data.identifiers.map(item => `<p>${escapeHtml(item.identifier_type)}：<code>${escapeHtml(item.identifier_value)}</code></p>`).join("") || '<p>无</p>'}</section></div>
    <section class="panel"><h3 class="panel-title">案件角色</h3>${data.case_roles.map(item => `<button class="command-case" data-phase2-case="${escapeHtml(item.case.case_id)}"><strong>${escapeHtml(item.case.case_name)}</strong><span>${escapeHtml(item.role.role_type)} · ${escapeHtml(item.case.case_number)}</span><em>进入案件 →</em></button>`).join("") || '<p>无可见案件</p>'}</section>
    <section class="panel"><h3 class="panel-title">关系、合并候选与冲突</h3><p>主体关系 ${data.relations.length} 条 · 合并候选 ${data.merge_candidates.length} 条 · 冲突 ${data.conflicts.length} 条</p>${data.conflicts.map(item => `<p><strong>${escapeHtml(item.field_name)}</strong> · ${escapeHtml(item.status)}</p>`).join("")}</section>
    <section class="panel"><h3 class="panel-title">数据来源</h3>${data.sources.map(item => `<p>${escapeHtml(item.source_type)} / ${escapeHtml(item.source_id)} · ${escapeHtml(item.source_field)}</p>`).join("") || '<p>无来源引用</p>'}</section>`);
}

function renderLiveForest(data) {
  const partyById = Object.fromEntries(data.parties.map(item => [item.party_id, item]));
  const rolesByCase = data.party_case_roles.reduce((map, role) => ((map[role.case_id] ||= []).push(role), map), {});
  const progressByCase = data.case_progress.reduce((map, item) => ((map[item.case_id] ||= []).push(item), map), {});
  content.innerHTML = `${sectionHeading("实时案件森林", "Party → PartyCaseRole → Case → CaseProgress；枝条直接来自 PostgreSQL")}
    ${renderBulkFollowupPanel(data)}
    <div class="case-forest live-forest">${data.cases.map(item => { const roles = rolesByCase[item.case_id] || []; const progress = progressByCase[item.case_id] || []; return `<details class="forest-root" open><summary><span>案件</span><strong>${escapeHtml(item.case_name)}</strong><em>${progress.filter(row => !row.deleted_at).length} 条有效进展</em></summary><div class="forest-branches"><div class="live-party-row">${roles.map(role => `<button data-live-party="${escapeHtml(role.party_id)}">${escapeHtml(role.role_type)} · ${escapeHtml(partyById[role.party_id]?.canonical_name || role.party_id)}</button>`).join("") || '无已确认主体'}</div>${progress.map(row => `<article class="live-progress ${row.deleted_at ? "deleted" : ""}"><strong>${escapeHtml(row.summary)}</strong><p>${escapeHtml(row.updated_at)} · v${row.version} ${row.deleted_at ? "· 已软删除" : ""}</p>${originLabel(row.data_origin)}</article>`).join("") || '<div class="empty-state">暂无 PostgreSQL 进展枝条</div>'}<button class="text-button" data-phase2-case="${escapeHtml(item.case_id)}">打开完整案件工作台 →</button></div></details>`; }).join("") || '<div class="empty-state">暂无案件</div>'}</div>`;
}

function renderBulkFollowupPanel(data) {
  const owners = [...new Map((data.identity_bindings || []).map(item => [
    item.user_id, item.display_name || item.user_id,
  ])).entries()];
  return `<details class="panel followup-bulk-panel">
    <summary><strong>批量配置案件追问</strong><span>先预览，明确确认后才写入；默认保留单案人工例外</span></summary>
    <form id="followup-bulk-form" class="followup-form-grid">
      <label>案件角色<select name="case_type"><option value="">全部</option><option value="plaintiff">原告</option><option value="defendant">被告</option></select></label>
      <label>负责人<select name="assigned_user_id"><option value="">全部</option>${owners.map(([id, name]) => `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`).join("")}</select></label>
      <label>一级阶段<input name="stage" placeholder="如：诉讼中、开庭" /></label>
      <label>目标频率<select name="cadence_type"><option value="daily">每天</option><option value="weekly" selected>每周</option><option value="every_15_days">每 15 天</option><option value="monthly">每月</option><option value="event_only">仅关键节点</option><option value="manual_only">仅人工追问</option><option value="paused">暂停</option><option value="disabled">关闭</option></select></label>
      <label class="followup-check"><input name="enabled" type="checkbox" checked />启用策略</label>
      <label class="followup-check"><input name="force_manual_override" type="checkbox" />强制覆盖单案人工例外</label>
      <button type="submit" class="primary-button">生成变更预览</button>
    </form>
    <div id="followup-bulk-preview" class="followup-preview empty-state">尚未生成预览，当前不会写入任何案件。</div>
  </details>`;
}

function renderLiveTravel(data) {
  content.innerHTML = `${sectionHeading("出差协同", "展示真实登记、同期同地匹配、通知和双方回复；平台接受不等于确认送达")}
    <div class="cards">${[["出差登记", data.summary.travels], ["协同候选", data.summary.candidates], ["通知记录", data.summary.notifications], ["等待回复", data.summary.waiting_for_reply]].map(([label, value]) => `<article class="metric-card"><span>${label}</span><strong>${value}<small> 条</small></strong></article>`).join("")}</div>
    <section class="panel"><h3 class="panel-title">出差登记</h3><div class="table-wrap"><table><thead><tr><th>人员</th><th>目的地</th><th>时间</th><th>事由</th><th>状态</th><th>来源</th></tr></thead><tbody>${data.travels.map(item => `<tr><td>${escapeHtml(item.traveler_name)}</td><td>${escapeHtml(item.destination)}</td><td>${formatBusinessTime(item.start_at)} 至 ${formatBusinessTime(item.end_at)}</td><td>${escapeHtml(item.purpose)}</td><td>${badge(item.status)}</td><td>${escapeHtml(item.source)}</td></tr>`).join("") || '<tr><td colspan="6">暂无出差登记</td></tr>'}</tbody></table></div></section>
    <section class="panel"><h3 class="panel-title">协同候选与回复</h3>${data.candidates.map(item => `<article class="travel-candidate-card"><div><strong>${escapeHtml(item.destination)}</strong><span class="badge">${escapeHtml(item.status)}</span></div><p>${formatBusinessTime(item.overlap_start)} 至 ${formatBusinessTime(item.overlap_end)}</p><p>参与人：${escapeHtml(item.participants.join("、"))}</p><div>${item.responses.map(response => `<span class="collaboration-chip">${escapeHtml(response.participant)}：${escapeHtml(response.status)}</span>`).join("")}</div></article>`).join("") || '<div class="empty-state">暂无协同候选</div>'}</section>
    <section class="panel"><h3 class="panel-title">通知状态</h3><div class="table-wrap"><table><thead><tr><th>接收人</th><th>发送状态</th><th>送达证据</th><th>发送时间</th><th>重试/错误</th></tr></thead><tbody>${data.notifications.map(item => `<tr><td>${escapeHtml(item.recipient_name)}</td><td>${badge(item.status)}</td><td>${escapeHtml(item.delivery_claim)}</td><td>${formatBusinessTime(item.sent_at)}</td><td>${item.retry_count} / ${escapeHtml(item.error || "无")}</td></tr>`).join("") || '<tr><td colspan="5">暂无通知</td></tr>'}</tbody></table></div></section>`;
}

function renderLiveAudit(data) {
  content.innerHTML = `${sectionHeading("审计证据", "展示业务操作、执行状态和写入结果；内部消息与数据库编号不在页面暴露")}
    <div class="table-wrap"><table><thead><tr><th>时间</th><th>命令</th><th>状态</th><th>资源类型</th><th>写入</th></tr></thead><tbody>${data.receipts.map(item => `<tr><td>${escapeHtml(item.created_at)}</td><td>${escapeHtml(auditCommandLabel(item.command_type))}</td><td>${businessBadge(auditStatusLabel(item.status))}</td><td>${escapeHtml(auditResourceLabel(item.resource_type))}</td><td>${item.actual_write ? "是" : "否"}</td></tr>`).join("")}</tbody></table></div>
    <section class="panel"><h3 class="panel-title">操作审计 <span>${data.audits.length} 条</span></h3>${data.audits.slice(0,30).map(item => `<article class="live-event"><strong>${escapeHtml(auditCommandLabel(item.command_type))} · ${escapeHtml(auditResourceLabel(item.resource_type))}</strong><p>${escapeHtml(item.created_at)}</p>${originLabel(item.data_origin)}</article>`).join("")}</section>`;
}

function renderBusinessFacts(facts) {
  return (facts || []).map(item => `<div><dt>${escapeHtml(item.label)}</dt><dd>${escapeHtml(item.value)}</dd></div>`).join("");
}

function sectionHeading(title, description, aside = "") {
  return `<div class="section-heading"><div><h2>${title}</h2><p>${description}</p></div>${aside}</div>`;
}

function renderOverview(data) {
  content.innerHTML = `
    ${sectionHeading("法务运营作战指挥中心", "三分钟识别团队偏差、案件风险和需要管理人员介入的事项",
      `<button class="screen-mode" id="screen-mode">进入大屏指挥模式</button>`)}
    <div class="cards">${data.cards.map(card => `
      <article class="metric-card command-card ${escapeHtml(card.status)}"><span>${escapeHtml(card.label)}</span>
        <strong>${card.value}<small>${escapeHtml(card.unit)}</small></strong>
        <p>${escapeHtml(card.target)} · ${escapeHtml(card.change)}</p>
        <small>更新：${escapeHtml(card.updated_at)}</small>
        <button data-route="${escapeHtml(card.drilldown)}">进入明细 →</button>
      </article>`).join("")}</div>
    <section class="panel command-matrix"><h3 class="panel-title">团队作战矩阵 <span>${escapeHtml(data.daily_date)} · 点击状态进入对应工作台</span></h3>
      <div class="table-wrap"><table><thead><tr><th>团队</th><th>周度指标</th><th>月度指标</th><th>目标填报</th><th>日报</th><th>案件推进</th><th>执行回款</th><th>风险</th><th>出差</th></tr></thead><tbody>
      ${data.team_matrix.map(row => `<tr><td><button class="text-button team-link" data-page-route="teams" data-team="${escapeHtml(row.team_id)}">${escapeHtml(row.team_name)}</button></td><td>${matrixState(row.weekly, "performance")}</td><td>${matrixState(row.monthly, "performance")}</td><td>${matrixState(row.target_collection, "performance")}</td><td>${matrixState(row.daily, "work")}</td><td>${matrixState(row.case_progress, "forest")}</td><td>${matrixState(row.recovery, "performance")}</td><td><button class="matrix-count" data-page-route="forest">${row.risk_count} 件</button></td><td><button class="matrix-count" data-page-route="travel">${row.travel_count} 人</button></td></tr>`).join("")}
      </tbody></table></div>
    </section>
    <div class="grid-2 command-lower">
      <section class="panel"><h3 class="panel-title">案件态势 <span>风险与回款</span></h3>
        <div class="case-posture"><div><strong>${data.case_status.open || 0}</strong><span>在办</span></div><div><strong>${data.risk_levels.high || 0}</strong><span>高风险</span></div><div><strong>3</strong><span>长期无进展</span></div><div><strong>¥120万</strong><span>本月回款</span></div></div>
        <button class="command-case" data-page-route="forest"><strong>华东建设有限公司</strong><span>关联 3 个案件 · 1 个执行预警 · 最近进展 24 天前</span><em>进入案件森林 →</em></button>
        <button class="command-case" data-page-route="forest"><strong>南京同名科技有限公司</strong><span>存在同名主体待核实 · 涉及 2 个案件</span><em>核实主体 →</em></button>
      </section>
      <section class="panel action-center"><h3 class="panel-title">行动中心 <span>${data.actions.length} 项需要处理</span></h3>
        ${data.actions.map(action => `<button data-page-route="${escapeHtml(action.route)}"><span class="action-type">${escapeHtml(action.type)}</span><strong>${escapeHtml(action.object)}</strong><p>${escapeHtml(action.reason)}</p><small>${escapeHtml(action.owner)} · ${escapeHtml(action.duration)}</small><em>${escapeHtml(action.status)} →</em></button>`).join("")}
      </section>
    </div>`;
  document.querySelector("#screen-mode")?.addEventListener("click", () => {
    document.body.classList.toggle("command-screen");
    document.querySelector("#screen-mode").textContent = document.body.classList.contains("command-screen") ? "退出大屏模式" : "进入大屏指挥模式";
  });
}

function matrixState(value, route) {
  const className = value === "正常" || value === "已确认" ? "matrix-ok" : value === "异常" || value === "未提交" ? "matrix-bad" : "matrix-warn";
  return `<button class="matrix-state ${className}" data-page-route="${route}">${escapeHtml(value)}</button>`;
}

function renderReport(data) {
  const labels = { daily: "工作与日报", weekly: "周报", monthly: "月报" };
  const people = new Set(data.submissions.map(item => item.user_id)).size;
  const periods = new Set(data.submissions.map(item => item.period)).size;
  content.innerHTML = `
    ${sectionHeading(labels[data.period_type], "从团队、人员和明细三个视角查看真实工作、修改记录及案件关联",
      `<div class="view-switch"><button class="active">团队视图</button><button>人员视图</button><button>明细视图</button></div>`)}
    <div class="cards">
      ${[["历史日报", data.summary.total, "份"], ["覆盖人员", people, "人"], ["覆盖日期", periods, "天"], ["待补充", data.summary.pending, "份"]].map(([label, value, unit]) => `<article class="metric-card"><span>${label}</span><strong>${value}<small>${unit}</small></strong></article>`).join("")}
    </div>
    <div class="daily-toolbar"><strong>近期工作记录</strong><span>包含修改、作废、案件与出差关联</span></div>
    <div class="table-wrap"><table><thead><tr><th>日期</th><th>提交人 / 团队</th><th>今日重点工作</th><th>明日计划</th><th>关联</th><th>状态</th></tr></thead><tbody>
      ${data.submissions.map(item => `<tr data-report="${escapeHtml(item.id)}" data-period="${escapeHtml(data.period_type)}"><td><strong>${escapeHtml(item.period)}</strong><br><small>${escapeHtml(item.source_display || "来源：日报机器人")}</small></td><td>${escapeHtml(item.user_name || businessUser(item.user_id))}<br><small>${escapeHtml(businessTeam(item.team_id))}</small></td><td>${escapeHtml(item.summary)}${item.revisions?.length ? '<span class="change-flag">有修改</span>' : ''}${item.deleted_items?.length ? '<span class="void-flag">有作废项</span>' : ''}</td><td>${escapeHtml(item.tomorrow_plan || "—")}</td><td><span class="link-count">${item.linked_case_ids?.length || 0} 案件</span>${item.linked_travel_ids?.length ? `<br><span class="link-count">${item.linked_travel_ids.length} 出差</span>` : ""}</td><td>${businessBadge(item.status)}<br><small>${escapeHtml(item.ai_split_status || "")}</small></td></tr>`).join("")}
    </tbody></table></div>`;
}

function renderPerformanceHub(data) {
  const weeklyMetrics = data.metrics.filter(item => item.period_type === "weekly");
  const monthlyMetrics = data.metrics.filter(item => item.period_type === "monthly");
  const weeklyReport = data.report_runs.find(item => item.period_type === "weekly");
  const monthlyReport = data.report_runs.find(item => item.period_type === "monthly");
  const replied = data.target_collections.filter(item => item.confirmation_status !== "未回复").length;
  content.innerHTML = `
    ${sectionHeading("绩效报告生产系统", "周度与月度使用独立模板和指标口径，不由日报逐级汇总",
      `<div class="view-switch"><button class="active">周度报告</button><button>月度报告</button><button>指标看板</button><button>目标收集</button><button>数据维护</button></div>`)}
    <div class="report-flow" aria-label="报告生产流程">
      ${["机器人发起收集", "负责人结构化回复", "管理人员确认", "固定格式报告引用"].map((item, index) => `<div><span>${index + 1}</span><strong>${item}</strong></div>`).join("")}
    </div>
    <div class="cards">
      <article class="metric-card"><span>周度指标</span><strong>${weeklyMetrics.length}<small> 项</small></strong><p>${escapeHtml(weeklyReport.status)}</p></article>
      <article class="metric-card"><span>月度指标</span><strong>${monthlyMetrics.length}<small> 项</small></strong><p>${escapeHtml(monthlyReport.status)}</p></article>
      <article class="metric-card"><span>目标收集</span><strong>${replied}<small> / ${data.target_collections.length} 组</small></strong><p>1 个团队已提醒仍未回复</p></article>
      <article class="metric-card warning-card"><span>人工维护</span><strong>${data.manual_entries.filter(item => item.status === "待确认").length}<small> 项待确认</small></strong><p>待确认数据不会进入正式报告</p></article>
    </div>
    <div class="grid-2">
      ${reportPreviewCard(weeklyReport, weeklyMetrics)}
      ${reportPreviewCard(monthlyReport, monthlyMetrics)}
    </div>
    <section class="panel"><h3 class="panel-title">指标来源与维护状态 <span>业务口径</span></h3>
      <div class="metric-source-grid">${data.source_types.map(source => `<article><span>${source.connected ? "已启用" : "数据暂未接入"}</span><strong>${escapeHtml(source.label)}</strong><p>${source.connected ? "按结构化口径进入报告" : "仅预留 Adapter，不伪造正式数据"}</p></article>`).join("")}</div>
    </section>
    <section class="panel"><h3 class="panel-title">目标收集 <span>机器人发起 → 负责人回复 → 管理人员确认 → 报告引用</span></h3>
      <div class="collection-grid">${data.target_collections.map(item => `<article class="collection-card"><div><strong>${escapeHtml(item.team_name)}</strong>${businessBadge(item.confirmation_status)}</div><p>${escapeHtml(item.leader)} · ${escapeHtml(item.reminder_status)}</p><dl><div><dt>目标值</dt><dd>${item.target_value ?? "待回复"}</dd></div><div><dt>实际值</dt><dd>${item.actual_value ?? "待回复"}</dd></div></dl><small>${escapeHtml(item.reason || item.risk || item.next_goal || "已完成结构化确认")}</small></article>`).join("")}</div>
    </section>
    <section class="panel"><h3 class="panel-title">数据维护 <span>仅显示需要人工维护的指标</span></h3>
      <div class="table-wrap"><table><thead><tr><th>团队</th><th>指标</th><th>目标值</th><th>实际值</th><th>维护人</th><th>说明</th><th>状态</th></tr></thead><tbody>${data.manual_entries.map(item => `<tr><td>${escapeHtml(businessTeam(item.team_id))}</td><td>${escapeHtml(item.metric_name)}</td><td>${Number(item.target_value).toLocaleString("zh-CN")}</td><td>${Number(item.actual_value).toLocaleString("zh-CN")}</td><td>${escapeHtml(item.maintainer)}</td><td>${escapeHtml(item.remark)}</td><td>${businessBadge(item.status)}</td></tr>`).join("")}</tbody></table></div>
    </section>`;
}

function reportPreviewCard(report, metrics) {
  return `<section class="panel report-preview"><h3 class="panel-title">${escapeHtml(report.title)} <span>${escapeHtml(report.period_start)} 至 ${escapeHtml(report.period_end)}</span></h3><p>${escapeHtml(report.overall_summary)}</p><div class="report-kpis">${metrics.slice(0,3).map(metric => `<div><span>${escapeHtml(metric.metric_name)}</span><strong>${metric.unit === "%" ? `${Math.round(metric.actual_value * 100)}%` : Number(metric.actual_value).toLocaleString("zh-CN")}</strong><small>${escapeHtml(metric.team_name)} · 完成率 ${Math.round(metric.completion_rate * 100)}%</small></div>`).join("")}</div><div class="report-alert"><strong>重点问题</strong><p>${escapeHtml(report.risk_summary)}</p></div><div class="export-actions"><button data-export="${escapeHtml(report.period_type)}:docx">导出 Word</button><button data-export="${escapeHtml(report.period_type)}:pdf">导出 PDF</button><button data-export="${escapeHtml(report.period_type)}:xlsx">导出 Excel</button></div></section>`;
}

function renderAdminHub([quality, sources, permissions]) {
  content.innerHTML = `${sectionHeading("系统管理", "开发验收、数据来源、权限和审计已移出业务主导航")}
    <div class="admin-grid">
      <section class="panel"><h3 class="panel-title">数据质量</h3><strong class="admin-number">${quality.issues.length}</strong><p>待复核的数据来源、冲突和外部线索</p></section>
      <section class="panel"><h3 class="panel-title">数据来源</h3><strong class="admin-number">${sources.sources.length}</strong><p>业务页面仅展示中文来源，技术契约保留在这里</p></section>
      <section class="panel"><h3 class="panel-title">当前权限</h3><strong>${escapeHtml(permissions.current_principal.user_id)}</strong><p>${permissions.current_principal.role_ids.map(businessBadge).join(" ")}</p></section>
    </div>`;
}

function businessUser(userId) {
  return state.shell?.users?.find(user => user.id === userId)?.name || userId;
}

function businessTeam(teamId) {
  return state.shell?.teams?.find(team => team.id === teamId)?.name || teamId;
}

function businessBadge(value) {
  const labels = {completed: "已完成", pending: "待补充", open: "在办", monitoring: "重点跟踪", closed: "已结案", high: "高风险", medium: "关注", low: "正常"};
  return `<span class="badge ${escapeHtml(value)}">${escapeHtml(labels[value] || value)}</span>`;
}

function renderTeams(data) {
  const teams = state.teamId ? data.teams.filter(team => team.id === state.teamId) : data.teams;
  content.innerHTML = `${sectionHeading("团队工作台", "按团队联动日报完成、案件负荷、停滞风险与出差协同")}
    <div class="team-grid">${teams.map(team => `<article class="team-card"><h3>${escapeHtml(team.name)}</h3><div class="mini-stats">
      <div><strong>${team.daily_completion_rate}%</strong><span>今日日报</span></div><div><strong>${team.case_count}</strong><span>案件</span></div><div><strong>${team.high_risk_count}</strong><span>高风险</span></div><div><strong>${team.stagnant_case_count}</strong><span>停滞案件</span></div>
    </div><p class="team-members">${team.members.map(member => escapeHtml(member.name)).join(" · ")}</p><div class="team-actions"><button class="text-button" data-team-cases="${escapeHtml(team.id)}">查看团队案件 →</button><button class="text-button" data-page-route="travel" data-team="${escapeHtml(team.id)}">${team.active_travel_count} 人出差 →</button></div></article>`).join("")}</div>`;
}

function renderTravel(data) {
  const travels = state.teamId ? data.travels.filter(item => item.team_id === state.teamId) : data.travels;
  const candidates = travels.reduce((sum, item) => sum + item.collaboration_candidates.length, 0);
  content.innerHTML = `${sectionHeading("出差协同", "识别时间与目的地重叠的同事，减少重复行程并直达关联案件")}
    <div class="cards"><article class="metric-card"><span>当前行程</span><strong>${travels.length}<small> 条</small></strong></article><article class="metric-card"><span>在途人员</span><strong>${travels.filter(item => item.status === "active").length}<small> 人</small></strong></article><article class="metric-card"><span>可协同行程</span><strong>${candidates}<small> 个匹配</small></strong></article></div>
    <div class="source-contract"><strong>来源：出差登记</strong><span>业务记录只读 · 每条行程保留原始来源</span></div>
    <div class="table-wrap"><table><thead><tr><th>出差人</th><th>目的地</th><th>日期</th><th>事由</th><th>协同建议</th><th>关联案件</th></tr></thead><tbody>
      ${travels.map(item => `<tr><td><strong>${escapeHtml(item.traveler_name)}</strong><br><small>${escapeHtml(businessTeam(item.team_id))}</small></td><td>${escapeHtml(item.destination)}</td><td>${escapeHtml(item.start_date)} – ${escapeHtml(item.end_date)}</td><td>${escapeHtml(item.purpose)}</td><td>${item.collaboration_candidates.length ? item.collaboration_candidates.map(peer => `<span class="collaboration-chip">可与 ${escapeHtml(peer.traveler_name)} 协同</span>`).join("") : '<span class="muted">暂无重叠行程</span>'}</td><td><button class="text-button" data-case="${escapeHtml(item.case_id)}">${escapeHtml(item.case_title)} →</button></td></tr>`).join("")}
    </tbody></table></div>`;
}

function renderMetrics(data) {
  content.innerHTML = `${sectionHeading("指标与绩效中心", "正式值只由后端指标注册表计算；草稿口径不会输出正式数值")}
    <div class="metric-list">${data.metrics.map(metric => `<article class="metric-row">
      <div><h3>${escapeHtml(metric.name)}</h3><p>${escapeHtml(metric.code)} · ${escapeHtml(metric.source_component)}</p></div>
      <div>${badge(metric.definition_status)}</div>
      <div class="metric-value">${metric.formal ? `${metric.value}<small>${escapeHtml(metric.unit)}</small>` : "—"}</div>
      <button class="text-button" data-metric="${escapeHtml(metric.id)}">${metric.formal ? `查看 ${metric.components.length} 个组成项 →` : "待业务确认"}</button>
    </article>`).join("")}</div>`;
  content.querySelectorAll("[data-metric]").forEach(button => button.addEventListener("click", () => {
    const metric = data.metrics.find(item => item.id === button.dataset.metric);
    openDrawer(`<p class="eyebrow">METRIC DEFINITION</p><h2>${escapeHtml(metric.name)}</h2><p>${badge(metric.definition_status)}</p>
      <div class="panel"><h3 class="panel-title">口径信息</h3><p>注册键：<code>${escapeHtml(metric.registry_key || "未注册")}</code></p><p>来源：<code>${escapeHtml(metric.source_id)}</code></p><p>${escapeHtml(metric.warning || "正式指标，由服务端计算")}</p></div>
      <div class="panel"><h3 class="panel-title">组成项 <span>${metric.components.length} 条</span></h3>${metric.components.map(c => `<p><code>${escapeHtml(c.resource_type)}:${escapeHtml(c.resource_id)}</code> = ${escapeHtml(c.value)}<br><small>${escapeHtml(c.team_id)} · ${escapeHtml(c.user_id)}</small></p>`).join("") || "<p>尚无正式组成项</p>"}</div>`);
  }));
}

async function loadCases(extra = {}) {
  const query = new URLSearchParams();
  const teamId = extra.teamId ?? state.teamId;
  if (teamId) query.set("team_id", teamId);
  if (extra.status) query.set("status", extra.status);
  if (extra.risk) query.set("risk", extra.risk);
  if (extra.query) query.set("query", extra.query);
  const data = await api(`cases?${query}`);
  content.innerHTML = `${sectionHeading("案件森林", `${data.total} 个案件正在按原告、被告和案件重组为可展开的业务森林`, `<div class="forest-zoom"><button data-forest-zoom="out">−</button><span>100%</span><button data-forest-zoom="in">＋</button></div>`)}
    <div class="toolbar"><input id="case-search" placeholder="按案件名称或编号检索" value="${escapeHtml(extra.query || "")}" />
      <select id="case-status"><option value="">全部状态</option>${["open","monitoring","closed"].map(v => `<option ${extra.status === v ? "selected" : ""}>${v}</option>`).join("")}</select>
      <select id="case-risk"><option value="">全部风险</option>${["high","medium","low"].map(v => `<option ${extra.risk === v ? "selected" : ""}>${v}</option>`).join("")}</select>
      <span class="badge">${data.total} 件</span></div>
    ${caseForest(data.items)}`;
  const apply = () => loadCases({ query: document.querySelector("#case-search").value, status: document.querySelector("#case-status").value, risk: document.querySelector("#case-risk").value });
  document.querySelector("#case-search").addEventListener("change", apply);
  document.querySelector("#case-status").addEventListener("change", apply);
  document.querySelector("#case-risk").addEventListener("change", apply);
  let zoom = 1;
  content.querySelectorAll("[data-forest-zoom]").forEach(button => button.addEventListener("click", () => {
    zoom = Math.max(.7, Math.min(1.4, zoom + (button.dataset.forestZoom === "in" ? .1 : -.1)));
    document.querySelector("#forest-canvas").style.transform = `scale(${zoom})`;
    document.querySelector(".forest-zoom span").textContent = `${Math.round(zoom * 100)}%`;
  }));
}

function caseForest(cases) {
  const roots = new Map();
  cases.forEach(item => {
    const plaintiff = item.plaintiff || "未归类原告";
    const root = roots.get(plaintiff) || new Map();
    (item.defendants || ["未归类被告"]).forEach(defendant => {
      const branch = root.get(defendant) || [];
      branch.push(item);
      root.set(defendant, branch);
    });
    roots.set(plaintiff, root);
  });
  if (!cases.length) return `<div class="empty-state"><strong>没有匹配案件</strong><p>请调整筛选条件。</p></div>`;
  return `<div class="forest-viewport"><div class="case-forest" id="forest-canvas">${[...roots].map(([plaintiff, defendants]) => `
    <details class="forest-root" open><summary><span>原告</span><strong>${escapeHtml(plaintiff)}</strong><em>${[...defendants.values()].flat().length} 件</em></summary>
      <div class="forest-branches">${[...defendants].map(([defendant, branch]) => `<details class="forest-branch" open><summary><span>被告</span><strong>${escapeHtml(defendant)}</strong><em>${branch.length} 件</em></summary>
        <div class="forest-leaves">${branch.map(item => `<button class="forest-case ${escapeHtml(item.risk_level)}" data-case="${escapeHtml(item.id)}"><span>${escapeHtml(item.case_number)}</span><strong>${escapeHtml(item.cause)}</strong><small>${escapeHtml(item.owner_name)} · 停滞 ${item.stagnation_days} 天</small><div>${badge(item.current_phase)} ${badge(item.risk_level)}</div></button>`).join("")}</div>
      </details>`).join("")}</div>
    </details>`).join("")}</div></div>`;
}

async function showCase(caseId) {
  content.innerHTML = `<div class="loading-card">正在加载案件工作台…</div>`;
  try {
    const item = await api(`cases/${encodeURIComponent(caseId)}`);
    const lanes = item.lifecycle.lanes.map(lane => {
      const nodes = item.lifecycle.nodes.filter(node => node.lane_id === lane.id);
      return `<section class="lane"><div class="lane-heading"><span>${escapeHtml(lane.name)}</span><span>${nodes.length} 个节点</span></div><div class="lane-nodes">${nodes.map(node => {
        const ai = node.origin.status.startsWith("ai_");
        return `<article class="life-node ${ai ? "ai" : ""}"><h4>${escapeHtml(node.title)}</h4>${badge(node.kind)} ${badge(node.origin.status)}<p>${escapeHtml(node.occurred_at)}</p><p>来源：${escapeHtml(node.origin.source_id)} · ${node.origin.confirmed ? "已确认" : "未确认"}</p></article>`;
      }).join("")}</div></section>`;
    }).join("");
    const timelineIds = item.lifecycle.timeline_node_ids || [];
    const timeline = timelineIds.map(id => item.lifecycle.nodes.find(node => node.id === id)).filter(Boolean);
    content.innerHTML = `<button class="back-to-forest" data-page-route="forest">← 返回案件森林</button><div class="case-header"><p class="eyebrow">CASE WORKSPACE</p><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.case_number || item.id)} · ${escapeHtml(item.cause || item.case_type)}</p><div class="case-meta">${badge(item.status)} ${badge(item.risk_level)} ${badge(item.origin.status)}</div></div>
      <div class="case-workspace-grid"><section class="panel"><h3 class="panel-title">案件推进</h3><p>承办人：${escapeHtml(item.owner_name || item.owner_user_id)}</p><p>最近进展：${escapeHtml(item.last_progress)} · 停滞 ${item.stagnation_days} 天</p><p>下一步：<strong>${escapeHtml(item.next_action)}</strong></p></section><section class="panel"><h3 class="panel-title">金额与执行</h3><p>诉请金额：¥ ${Number(item.amount).toLocaleString("zh-CN")}</p><p>支持金额：¥ ${Number(item.supported_amount).toLocaleString("zh-CN")}</p><p>已回款：¥ ${Number(item.recovered_amount).toLocaleString("zh-CN")}</p><p>${escapeHtml(item.execution_status)}</p></section></div>
      <div class="timeline"><strong>程序时间轴</strong>${timeline.map(node => `<div><span></span><p>${escapeHtml(node.title)}<small>${escapeHtml(node.occurred_at)}</small></p></div>`).join("")}</div>
      <div class="lifecycle">${lanes}</div><p class="origin-help">蓝色节点为 AI 派生内容，不能作为系统事实；每个节点保留来源、确认状态与生成信息。</p>`;
  } catch (error) {
    content.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

async function showReport(period, submissionId) {
  openDrawer(`<div class="loading-card">正在加载原始提交…</div>`);
  try {
    const item = await api(`reports/${encodeURIComponent(period)}/${encodeURIComponent(submissionId)}`);
    drawerContent.innerHTML = `<div class="case-header"><p class="eyebrow">${escapeHtml(item.period)} · 工作日报</p><h2>${escapeHtml(item.user_name || businessUser(item.user_id))}</h2><p>${escapeHtml(businessTeam(item.team_id))} · ${escapeHtml(item.source_display || "来源：日报机器人")}</p><div class="case-meta">${businessBadge(item.status)} <span class="badge">${escapeHtml(item.ai_split_status || "人工记录")}</span></div></div>
      <div class="panel"><h3 class="panel-title">原始对话</h3><blockquote class="raw-dialogue">${escapeHtml(item.raw_text)}</blockquote></div>
      <div class="panel"><h3 class="panel-title">Agent 拆分后的工作项</h3>${(item.work_items || []).map(work => `<article class="work-item"><strong>${escapeHtml(work.text)}</strong><p>${escapeHtml(work.work_type)} · ${escapeHtml(work.status)}</p><button class="text-button" data-case="${escapeHtml(work.linked_case_id)}">进入关联案件 →</button></article>`).join("")}</div>
      <div class="panel"><h3 class="panel-title">明日计划</h3><p>${escapeHtml(item.tomorrow_plan || "未填写")}</p></div>
      ${(item.revisions || []).length ? `<div class="panel"><h3 class="panel-title">修改记录</h3>${item.revisions.map(row => `<p><strong>${escapeHtml(row.action)}</strong> · ${escapeHtml(row.at)}<br><del>${escapeHtml(row.before)}</del><br>${escapeHtml(row.after)}</p>`).join("")}</div>` : ""}
      ${(item.deleted_items || []).length ? `<div class="panel"><h3 class="panel-title">作废记录</h3>${item.deleted_items.map(row => `<p><del>${escapeHtml(row.text)}</del><br><small>${escapeHtml(row.reason)} · ${escapeHtml(row.deleted_at)}</small></p>`).join("")}</div>` : ""}
      <div class="panel"><h3 class="panel-title">关联案件</h3>${(item.linked_case_ids || []).map(caseId => `<button class="text-button" data-case="${escapeHtml(caseId)}">进入案件森林 →</button>`).join("") || "<p>无关联案件</p>"}</div>`;
  } catch (error) {
    drawerContent.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderQuality(data) {
  content.innerHTML = `${sectionHeading("数据质量与异常", "集中展示来源缺失、AI 未确认、外部线索与隔离校验结果")}
    <div class="source-contract"><strong>租户引用校验</strong>${badge(data.isolation_verification.valid ? "confirmed" : "conflicted")}<span>${data.isolation_verification.violations.length} 个违规引用</span></div>
    <div class="table-wrap"><table><thead><tr><th>级别</th><th>异常代码</th><th>说明</th><th>对象</th><th>来源</th></tr></thead><tbody>${data.issues.map(item => `<tr><td>${badge(item.severity)}</td><td><code>${escapeHtml(item.code)}</code></td><td>${escapeHtml(item.message)}</td><td>${escapeHtml(item.resource_type)}:${escapeHtml(item.resource_id)}</td><td>${escapeHtml(item.source_id)}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderSources(data) {
  content.innerHTML = `${sectionHeading("来源与指标口径", "查看数据适配器、刷新状态、事实来源分类与指标定义状态")}
    <div class="panel"><h3 class="panel-title">允许的来源状态 <span>AI 与事实严格区分</span></h3><div>${data.origin_statuses.map(badge).join(" ")}</div></div>
    <div class="table-wrap"><table><thead><tr><th>数据源</th><th>适配器</th><th>模式</th><th>来源状态</th><th>刷新时间</th></tr></thead><tbody>${data.sources.map(item => `<tr><td><strong>${escapeHtml(item.name)}</strong><br><small>${escapeHtml(item.id)}</small></td><td><code>${escapeHtml(item.adapter)}</code></td><td>${badge(item.mode)}</td><td>${badge(item.origin_status)}</td><td>${escapeHtml(item.refreshed_at)}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderPermissions(data) {
  const p = data.current_principal;
  content.innerHTML = `${sectionHeading("租户与权限入口", "所有边界由服务端凭证目录解析，不信任 URL、查询参数或租户请求头")}
    <div class="grid-2"><div class="panel"><h3 class="panel-title">当前服务端身份</h3><p>Tenant：<code>${escapeHtml(p.tenant_id)}</code></p><p>User：<code>${escapeHtml(p.user_id)}</code></p><p>Roles：${p.role_ids.map(badge).join(" ")}</p><p>${escapeHtml(data.boundary)}</p></div>
    <div class="panel"><h3 class="panel-title">权限角色</h3>${data.roles.map(role => `<p><strong>${escapeHtml(role.name)}</strong><br><small>${role.permissions.map(escapeHtml).join(" · ")}</small></p>`).join("")}</div></div>`;
}

function renderAgent2(data) {
  const route = data.route_control || {};
  const summaryCards = [
    ["主体", data.summary.parties],
    ["案件关系", data.summary.party_case_roles],
    ["主体关系", data.summary.party_relations],
    ["业务线索", data.summary.party_case_clues],
    ["出差意图", data.summary.travel_intents],
    ["协同候选", data.summary.collaboration_candidates],
    ["通知", data.summary.notifications],
    ["案件进展", data.summary.case_progress],
    ["周/月报", data.summary.periodic_reports],
    ["回执", data.summary.receipts],
    ["失败 / 阻断", data.summary.failures],
  ];
  const compactRows = (items, columns) => items.length ? `
    <div class="table-wrap"><table><thead><tr>${columns.map(([label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead>
    <tbody>${items.map(item => `<tr>${columns.map(([, key]) => `<td>${escapeHtml(Array.isArray(item[key]) ? item[key].join("、") : item[key])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
    : `<div class="empty-state">暂无记录</div>`;
  const caseRows = data.cases.length ? `
    <div class="table-wrap"><table><thead><tr><th>案件</th><th>案号</th><th>状态</th><th>负责人</th><th>详情</th></tr></thead>
    <tbody>${data.cases.map(item => `<tr><td>${escapeHtml(item.case_name)}</td><td>${escapeHtml(item.case_number || item.external_case_id)}</td><td>${badge(item.status)}</td><td>${escapeHtml(item.owner_user_id)}</td><td><button class="text-button" data-phase2-case="${escapeHtml(item.case_id)}">生命树 →</button></td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty-state">暂无案件</div>`;
  content.innerHTML = `
    ${sectionHeading("Agent2 Phase 2 验收证据", "所有数据均从 Phase 2 PostgreSQL 表按当前租户只读加载")}
    <div class="source-contract"><strong>测试租户路由</strong>${badge(route.route_mode || "not_configured")}
      <span>Agent1 rollback：${route.agent1_rollback_enabled ? "显式开启" : "默认关闭"}</span>
      <span>version=${escapeHtml(route.version || 0)}</span></div>
    <div class="cards">${summaryCards.map(([label, value]) => `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${value}<small>条</small></strong></article>`).join("")}</div>
    <div class="panel"><h3 class="panel-title">主体与案件角色 <span>模糊候选不作为事实</span></h3>
      ${compactRows(data.parties, [["标准名称", "canonical_name"], ["类型", "party_type"], ["信用代码", "unified_social_credit_code"], ["数据质量", "data_quality"]])}</div>
    <div class="panel"><h3 class="panel-title">关联案件 <span>进入案件生命树核对内部进展、来源与审计</span></h3>${caseRows}</div>
    <div class="panel"><h3 class="panel-title">主体关系与案件线索 <span>人员、法院、回款、资产均保留案件和来源</span></h3>
      ${compactRows(data.party_case_clues, [["案件", "case_id"], ["主体", "party_id"], ["类型", "clue_type"], ["摘要", "summary"], ["金额", "amount"], ["来源", "source_type"]])}</div>
    <div class="panel"><h3 class="panel-title">出差协同与通知 <span>真实 transport receipt</span></h3>
      ${compactRows(data.notifications, [["状态", "status"], ["重试", "retry_count"], ["错误", "error_message"]])}</div>
    <div class="panel"><h3 class="panel-title">案件进展 <span>软删除、版本、来源</span></h3>
      ${compactRows(data.case_progress, [["案件", "case_id"], ["摘要", "summary"], ["来源", "content_origin"], ["版本", "version"], ["记录人", "reporter_id"]])}</div>
    <div class="panel"><h3 class="panel-title">周报 / 月报 <span>真实 PostgreSQL · 每次更新返回完整快照</span></h3>
      ${compactRows(data.periodic_reports, [["类型", "report_type"], ["周期", "period_key"], ["状态", "status"], ["版本", "version"], ["填报人", "owner_user_id"], ["来源", "data_origin"]])}</div>
    <div class="panel"><h3 class="panel-title">命令回执 <span>每个动作独立结果</span></h3>
      ${compactRows(data.receipts, [["命令", "command_type"], ["状态", "status"], ["资源类型", "resource_type"], ["实际写入", "actual_write"], ["失败阶段", "failed_stage"]])}</div>
    <div class="panel"><h3 class="panel-title">审计记录 <span>操作人、来源消息与资源可追溯</span></h3>
      ${compactRows(data.audits, [["命令", "command_type"], ["资源类型", "resource_type"], ["数据来源", "data_origin"], ["时间", "created_at"]])}</div>`;
}

async function showPhase2Case(caseId) {
  const data = await api(`workspace/cases/${encodeURIComponent(caseId)}`);
  renderPhase2Case(data);
}

function renderPhase2Case(data) {
  const progress = data.progress.map(item => `<article class="timeline-item"><strong>${escapeHtml(item.content)}</strong><p>${formatBusinessTime(item.occurred_at)} · ${escapeHtml(item.origin)}</p>${item.actions?.can_edit ? `<span class="inline-actions"><button type="button" data-start-case-progress-edit>修改</button><button type="button" class="danger-text" data-delete-case-progress data-case-id="${escapeHtml(data.case_ref)}" data-progress-ref="${escapeHtml(item.actions.progress_ref)}" data-version="${item.actions.expected_version}">删除</button></span><form class="inline-edit-form hidden" data-edit-case-progress-form data-case-id="${escapeHtml(data.case_ref)}" data-progress-ref="${escapeHtml(item.actions.progress_ref)}" data-version="${item.actions.expected_version}"><input name="summary" maxlength="2000" required value="${escapeHtml(item.content)}" aria-label="修改案件进展" /><button class="primary-button" type="submit">保存修改</button><button type="button" data-cancel-inline-edit>取消</button></form>` : ""}</article>`).join("") || '<div class="empty-state">暂无有效案件进展</div>';
  const audit = (data.audit || []).map(item => `<article class="timeline-item"><strong>${escapeHtml(item.action)} · ${escapeHtml(item.result)}</strong><p>${formatBusinessTime(item.created_at)} · ${escapeHtml(item.actor_name)} · ${escapeHtml(item.source)}</p></article>`).join("") || '<div class="empty-state">暂无操作记录</div>';
  const reportProjection = (data.report_projection || []).map(item => `<article class="timeline-item"><strong>${escapeHtml(item.report_type)} · ${escapeHtml(item.section)}</strong><p>${escapeHtml(item.status)} · ${formatBusinessTime(item.created_at)}</p></article>`).join("") || '<div class="empty-state">当前案件暂无日报投影</div>';
  const parties = data.parties.map(item => `<li><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.role)}</span></li>`).join("") || '<li>暂未记录当事人</li>';
  const clues = data.clues.map(item => `<li><strong>${escapeHtml(item.type)}</strong><span>${escapeHtml(item.summary)}</span></li>`).join("") || '<li>暂无结构化案件线索</li>';
  const progressForm = data.can_add_progress
    ? `<form class="case-progress-form" data-add-case-progress data-case-id="${escapeHtml(data.case_ref)}"><label>本次进展<textarea name="summary" maxlength="2000" required placeholder="按事实填写本次案件进展，不会由模型改写"></textarea></label><label>补充说明<textarea name="details" maxlength="10000" placeholder="可选"></textarea></label><label>下一步计划<input name="next_action" maxlength="2000" placeholder="可选，例如：下周联系法院确认查控结果" /></label><button class="primary-button" type="submit">记录案件进展</button></form>`
    : '<p class="muted report-readonly">当前凭证对该案件仅有查看权限，不能登记案件进展。</p>';
  openDrawer(`<div class="case-header"><p class="eyebrow">单案工作台</p><h2>${escapeHtml(data.case_name)}</h2><p>${escapeHtml(data.case_number)}</p><div class="case-meta"><span class="badge">${escapeHtml(data.case_type)}</span><span class="badge">${escapeHtml(data.stage)}</span><span class="badge">${escapeHtml(data.assignment)}</span><span class="badge">负责人：${escapeHtml(data.owner_name)}</span><span class="badge">${data.can_add_progress ? "可登记进展" : "仅查看"}</span></div></div>
    <nav class="case-detail-tabs" aria-label="案件详情区块">
      <button type="button" class="active" aria-current="true" data-case-section-target="case-overview">概览</button>
      <button type="button" data-case-section-target="case-lifecycle">生命周期</button>
      <button type="button" data-case-section-target="case-progress">进展与计划</button>
      <button type="button" data-case-section-target="case-parties">当事人</button>
      <button type="button" data-case-section-target="case-audit">操作记录</button>
      <button type="button" data-case-section-target="case-report-projection">日报投影</button>
      <button type="button" data-case-section-target="case-followup">主动追问</button>
    </nav>
    <section class="panel" id="case-overview"><h3 class="panel-title">案件概览</h3><div class="followup-status-grid"><div><span>当前节点</span><strong>${escapeHtml(data.current_node)}</strong></div><div><span>当前状态</span><strong>${escapeHtml(data.current_status)}</strong></div><div><span>开庭准备</span><strong>${escapeHtml(data.hearing_readiness)}</strong></div><div><span>数据来源</span><strong>${escapeHtml(data.source)}</strong></div><div><span>下一步计划</span><strong>${escapeHtml(data.next_plan.join("；") || "暂未记录")}</strong></div>${renderBusinessFacts(data.business_facts)}</div></section>
    <section class="panel" id="case-lifecycle"><h3 class="panel-title">案件生命周期</h3><div class="product-lifecycle">${data.lifecycle.map(item => `<div class="${escapeHtml(item.state)}"><span></span><strong>${escapeHtml(item.label)}</strong><small>${item.state === "completed" ? "已到达" : item.state === "current" ? "当前阶段" : "尚未到达"}</small></div>`).join("")}</div></section>
    <section class="panel" id="case-progress"><h3 class="panel-title">案件进展</h3>${progressForm}${progress}</section>
    <section class="panel" id="case-parties"><h3 class="panel-title">当事人与案件线索</h3><div class="grid-2"><ul class="detail-list">${parties}</ul><ul class="detail-list">${clues}</ul></div></section>
    <section class="panel" id="case-audit"><h3 class="panel-title">操作记录 <span>仅展示已写入的业务操作</span></h3>${audit}</section>
    <section class="panel" id="case-report-projection"><h3 class="panel-title">日报投影 <span>案件事实与报告条目分别保留</span></h3>${reportProjection}</section>
    <section class="panel" id="case-followup"><h3 class="panel-title">主动追问 <span>${data.followup.waiting_for_reply ? "等待负责人回复" : "当前无待回复任务"}</span></h3><div class="followup-status-grid"><div><span>当前策略</span><strong>${escapeHtml(data.followup.cadence)}</strong></div><div><span>下次追问</span><strong>${formatBusinessTime(data.followup.next_due_at)}</strong></div><div><span>最近追问</span><strong>${formatBusinessTime(data.followup.last_followup_at)}</strong></div><div><span>最近通知</span><strong>${escapeHtml(data.followup.last_message_status)}</strong></div></div>${renderProductFollowupManagement(data)}</section>`);
}

function activateCaseSection(button) {
  const target = drawerContent.querySelector(`#${button.dataset.caseSectionTarget}`);
  if (!target) return;
  drawerContent.querySelectorAll("[data-case-section-target]").forEach(item => {
    const active = item === button;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "true");
    else item.removeAttribute("aria-current");
  });
  target.scrollIntoView({behavior: "smooth", block: "start"});
}

function renderProductFollowupManagement(data) {
  if (!data.can_manage_followup || !data.management) return '<p class="muted">追问策略由管理员在权限范围内配置。</p>';
  const management = data.management;
  const capability = data.followup?.capability || {};
  const options = [["daily", "每天一次"], ["weekly", "每周一次"], ["every_15_days", "每 15 天一次"], ["monthly", "每月一次"], ["event_only", "仅关键节点"], ["manual_only", "仅人工追问"], ["paused", "暂停"], ["disabled", "关闭"]];
  const sendNotice = capability.message_delivery === "已开启"
    ? '<p class="muted">任务创建后会进入发送流程，最终状态以消息回执为准。</p>'
    : '<p class="muted">当前仅生成追问任务，钉钉发送未开启。</p>';
  const triggerButton = capability.can_trigger_task
    ? `<button type="button" data-trigger-followup-now data-case-id="${escapeHtml(management.case_ref)}" data-owner-id="${escapeHtml(management.owner_ref)}">${capability.message_delivery === "已开启" ? "创建追问任务并进入发送流程" : "创建追问任务（暂不发送）"}</button>`
    : '<button type="button" disabled>追问任务功能未开启</button>';
  return `${sendNotice}<form class="followup-policy-form" data-case-id="${escapeHtml(management.case_ref)}" data-owner-id="${escapeHtml(management.owner_ref)}" data-version="${management.expected_version}"><label>追问频率<select name="cadence_type">${options.map(([value, label]) => `<option value="${value}" ${management.cadence_type === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><label>自定义间隔（天）<input name="custom_interval_days" type="number" min="1" max="365" /></label><label class="followup-check"><input name="enabled" type="checkbox" ${management.enabled ? "checked" : ""} />启用追问策略</label><label class="followup-check"><input name="hearing_reminders_enabled" type="checkbox" ${management.hearing_reminders_enabled ? "checked" : ""} />开庭提醒</label><label class="followup-check"><input name="stage_transition_enabled" type="checkbox" ${management.stage_transition_enabled ? "checked" : ""} />阶段转换提醒</label><label class="followup-check"><input name="node_transition_enabled" type="checkbox" ${management.node_transition_enabled ? "checked" : ""} />关键节点提醒</label><button type="submit" class="primary-button">保存追问策略</button>${triggerButton}</form>`;
}

function renderCaseFollowupConfiguration(data) {
  const policy = data.policy || {};
  const status = data.latest_status || {};
  const lifecycle = data.lifecycle_state || {};
  const cadence = policy.cadence_type || "event_only";
  const version = Number(policy.version || 0);
  const options = [
    ["daily", "每天一次"], ["weekly", "每周一次"],
    ["every_15_days", "每 15 天一次"], ["monthly", "每月一次"],
    ["custom_interval", "自定义天数"], ["event_only", "仅关键节点"],
    ["manual_only", "仅人工追问"], ["paused", "暂停"], ["disabled", "关闭"],
  ];
  const history = (data.history || []).slice(0, 8).map(item => `
    <article class="followup-history-item"><strong>${escapeHtml(item.trigger_type)} · ${escapeHtml(item.task_status)}</strong>
      <p>${escapeHtml(item.question_text || "未生成问法")}</p>
      <small>${escapeHtml(item.created_at)} · 消息 ${escapeHtml(item.message_status)} · 回复 ${escapeHtml(item.response_status)}</small>
      ${["scheduled", "queued"].includes(item.task_status) ? `<button type="button" data-cancel-followup data-case-id="${escapeHtml(data.case.case_id)}" data-owner-id="${escapeHtml(data.case.owner_user_id)}" data-followup-id="${escapeHtml(item.followup_id)}" data-version="${escapeHtml(item.version)}">取消尚未发送的任务</button>` : ""}</article>`).join("") || '<div class="empty-state">尚无追问历史</div>';
  return `<section class="panel followup-policy-panel" data-followup-case="${escapeHtml(data.case.case_id)}">
    <h3 class="panel-title">案件主动追问 <span>${status.waiting_for_reply ? "正在等待负责人回复" : "当前无待回复任务"}</span></h3>
    <div class="followup-status-grid">
      <div><span>负责人</span><strong>${escapeHtml(data.case.owner_user_id)}</strong></div>
      <div><span>当前阶段 / 节点</span><strong>${escapeHtml([lifecycle.stage, lifecycle.node].filter(Boolean).join(" / ") || "尚未记录")}</strong></div>
      <div><span>当前状态</span><strong>${escapeHtml(lifecycle.current_status || "尚未记录")}</strong></div>
      <div><span>下一步计划</span><strong>${escapeHtml((lifecycle.next_actions_json || []).join("；") || "尚未记录")}</strong></div>
      <div><span>开庭准备</span><strong>${escapeHtml(lifecycle.hearing_readiness || "尚未记录")}</strong></div>
      <div><span>最近追问</span><strong>${escapeHtml(status.last_followup_at || "尚未追问")}</strong></div>
      <div><span>下次追问</span><strong>${escapeHtml(status.next_due_at || "尚未排期")}</strong></div>
      <div><span>消息状态</span><strong>${escapeHtml(status.last_message_status || "无")}</strong></div>
    </div>
    <form class="followup-policy-form" data-case-id="${escapeHtml(data.case.case_id)}" data-owner-id="${escapeHtml(data.case.owner_user_id)}" data-version="${version}">
      <label>追问频率<select name="cadence_type">${options.map(([value, label]) => `<option value="${value}" ${cadence === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      <label>自定义间隔（天）<input name="custom_interval_days" type="number" min="1" max="365" value="${escapeHtml(policy.custom_interval_json?.days || "")}" /></label>
      <label class="followup-check"><input name="enabled" type="checkbox" ${policy.enabled !== false ? "checked" : ""} />启用主动追问</label>
      <label class="followup-check"><input name="hearing_reminders_enabled" type="checkbox" ${policy.hearing_reminders_enabled !== false ? "checked" : ""} />开庭提醒</label>
      <label class="followup-check"><input name="stage_transition_enabled" type="checkbox" ${policy.stage_transition_enabled !== false ? "checked" : ""} />阶段转换提醒</label>
      <label class="followup-check"><input name="node_transition_enabled" type="checkbox" ${policy.node_transition_enabled !== false ? "checked" : ""} />关键节点提醒</label>
      <button type="submit" class="primary-button">保存单案策略</button>
      <button type="button" data-trigger-followup-now data-case-id="${escapeHtml(data.case.case_id)}" data-owner-id="${escapeHtml(data.case.owner_user_id)}">创建追问任务（发送状态以回执为准）</button>
    </form>
    <details class="followup-history"><summary>查看追问历史（${(data.history || []).length}）</summary>${history}</details>
  </section>`;
}

function followupRequestIdentity(prefix) {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return {
    source_turn_id: `legal-ops:${prefix}:${id}`,
    idempotency_key: `legal-ops:${prefix}:${id}`,
  };
}

async function saveCaseFollowupPolicy(form) {
  const cadenceType = form.elements.cadence_type.value;
  const customDays = Number(form.elements.custom_interval_days.value || 0);
  const identity = followupRequestIdentity("single-policy");
  const result = await apiMutation(
    `phase2/cases/${encodeURIComponent(form.dataset.caseId)}/followup`,
    "PUT",
    {
      assigned_user_id: form.dataset.ownerId,
      expected_version: Number(form.dataset.version),
      cadence_type: cadenceType,
      enabled: form.elements.enabled.checked,
      custom_interval_days: cadenceType === "custom_interval" ? customDays : null,
      hearing_reminders_enabled: form.elements.hearing_reminders_enabled.checked,
      stage_transition_enabled: form.elements.stage_transition_enabled.checked,
      node_transition_enabled: form.elements.node_transition_enabled.checked,
      force_manual_override: true,
      ...identity,
    },
  );
  if (!result.actual_write) throw new Error("策略没有写入");
  toast("单案追问策略已保存");
  await showPhase2Case(form.dataset.caseId);
}

async function triggerCaseFollowupNow(button) {
  button.disabled = true;
  try {
    const identity = followupRequestIdentity("trigger-now");
    const result = await apiMutation(
      `phase2/cases/${encodeURIComponent(button.dataset.caseId)}/followup/trigger-now`,
      "POST",
      { assigned_user_id: button.dataset.ownerId, ...identity },
    );
    if (!result.actual_write) throw new Error("本次没有创建新的追问任务");
    toast("追问任务已创建；是否发送仍由消息开关和发送回执决定");
    await showPhase2Case(button.dataset.caseId);
  } finally {
    button.disabled = false;
  }
}

async function cancelCaseFollowup(button) {
  button.disabled = true;
  try {
    const identity = followupRequestIdentity("cancel-task");
    const result = await apiMutation(
      `phase2/cases/${encodeURIComponent(button.dataset.caseId)}/followup/cancel`,
      "POST",
      {
        followup_id: button.dataset.followupId,
        assigned_user_id: button.dataset.ownerId,
        expected_version: Number(button.dataset.version),
        ...identity,
      },
    );
    if (!result.actual_write) throw new Error("任务没有被取消");
    toast("尚未发送的追问任务已取消");
    await showPhase2Case(button.dataset.caseId);
  } finally {
    button.disabled = false;
  }
}

function bulkFollowupPayload(form) {
  return {
    filters: {
      case_type: form.elements.case_type.value,
      stage: form.elements.stage.value.trim(),
      assigned_user_id: form.elements.assigned_user_id.value,
      risk_level: form.elements.risk_level.value,
    },
    change: {
      cadence_type: form.elements.cadence_type.value,
      enabled: form.elements.enabled.checked,
      force_manual_override: form.elements.force_manual_override.checked,
    },
  };
}

async function previewBulkFollowup(form) {
  const payload = bulkFollowupPayload(form);
  const preview = await apiMutation("phase2/followup/bulk-preview", "POST", payload);
  state.bulkFollowupPreview = { preview, payload };
  const target = document.querySelector("#followup-bulk-preview");
  const rows = preview.items.slice(0, 80).map(item => `<tr><td>${escapeHtml(item.case_name)}</td><td>${escapeHtml(item.assigned_user_id)}</td><td>${escapeHtml(item.before.cadence_type)}</td><td>${escapeHtml(item.after.cadence_type)}</td><td>${item.will_change ? "将修改" : escapeHtml(item.skip_reason)}</td></tr>`).join("");
  target.classList.remove("empty-state");
  target.innerHTML = `<div class="followup-preview-summary"><strong>匹配 ${preview.matched_count} 件，将修改 ${preview.change_count} 件</strong><span>保留单案人工例外 ${preview.preserved_manual_override_count} 件</span></div>
    <div class="table-wrap"><table><thead><tr><th>案件</th><th>负责人</th><th>修改前</th><th>修改后</th><th>结果</th></tr></thead><tbody>${rows}</tbody></table></div>
    <label class="followup-confirm"><input id="followup-bulk-confirm" type="checkbox" />我已核对预览，确认按上述范围执行</label>
    <button class="primary-button" type="button" data-apply-followup-bulk>确认执行批量修改</button>`;
}

async function applyBulkFollowup() {
  const checked = document.querySelector("#followup-bulk-confirm")?.checked;
  if (!checked) throw new Error("请先勾选明确确认");
  const saved = state.bulkFollowupPreview;
  if (!saved) throw new Error("预览已失效，请重新生成");
  const identity = followupRequestIdentity("bulk-policy");
  const result = await apiMutation("phase2/followup/bulk-apply", "POST", {
    ...saved.payload,
    preview_id: saved.preview.preview_id,
    confirm: true,
    ...identity,
  });
  state.bulkFollowupPreview = null;
  toast(`批量策略完成：成功 ${result.executed_count}，失败 ${result.failed_count}，跳过 ${result.skipped_count}`);
  await loadPage();
}

const operationId = prefix => `${prefix}-${crypto.randomUUID()}`;

function mutationMessage(result, successText) {
  if (result.actual_write) return successText;
  if (result.business_status === "duplicate") return "该操作已经处理过，本次没有重复写入。";
  return "本次没有产生数据变更。";
}

async function addCaseProgress(form) {
  const values = new FormData(form);
  const caseId = form.dataset.caseId;
  const result = await apiMutation(`workspace/cases/${encodeURIComponent(caseId)}/progress`, "POST", {
    summary: String(values.get("summary") || "").trim(),
    details: String(values.get("details") || "").trim(),
    next_actions: String(values.get("next_action") || "").trim() ? [String(values.get("next_action")).trim()] : [],
    operation_id: operationId("case-progress-create"),
  });
  toast(mutationMessage(result, "案件进展已记录。"));
  await reconcileCaseMutation(result.case);
}

async function editCaseProgress(form) {
  const replacement = String(new FormData(form).get("summary") || "").trim();
  if (!replacement) throw new Error("案件进展不能为空");
  const caseId = form.dataset.caseId;
  const result = await apiMutation(`workspace/cases/${encodeURIComponent(caseId)}/progress/${encodeURIComponent(form.dataset.progressRef)}`, "PUT", {
    expected_version: Number(form.dataset.version),
    summary: replacement,
    operation_id: operationId("case-progress-update"),
  });
  toast(mutationMessage(result, "案件进展已修改。"));
  await reconcileCaseMutation(result.case);
}

async function removeCaseProgress(button) {
  if (!requireSecondClick(button, "再次点击确认删除", "请再次点击确认删除；案件本身和其他进展不会受影响。")) return;
  const caseId = button.dataset.caseId;
  const result = await apiMutation(`workspace/cases/${encodeURIComponent(caseId)}/progress/${encodeURIComponent(button.dataset.progressRef)}`, "DELETE", {
    expected_version: Number(button.dataset.version),
    reason: "用户从案件工作台删除",
    operation_id: operationId("case-progress-delete"),
  });
  toast(mutationMessage(result, "案件进展已删除。"));
  await reconcileCaseMutation(result.case);
}

async function reconcileCaseMutation(caseData) {
  if (state.page === "cases") await loadLiveCases(state.casePage || 1);
  renderPhase2Case(caseData);
}

async function appendReportItem(form) {
  const values = new FormData(form);
  const result = await apiMutation(`workspace/reports/${encodeURIComponent(form.dataset.reportRef)}/commands`, "POST", {
    command_type: "append_item",
    expected_version: Number(form.dataset.version),
    field_name: String(values.get("field_name") || ""),
    value: String(values.get("value") || "").trim(),
    operation_id: operationId("report-append"),
  });
  toast(mutationMessage(result, "报告条目已新增。"));
  renderLiveReports(await api("workspace/reports"));
}

async function editReportItem(form) {
  const replacement = String(new FormData(form).get("value") || "").trim();
  if (!replacement) throw new Error("报告条目不能为空");
  const result = await apiMutation(`workspace/reports/${encodeURIComponent(form.dataset.reportRef)}/commands`, "POST", {
    command_type: "edit_item",
    expected_version: Number(form.dataset.version),
    item_ref: form.dataset.itemRef,
    value: replacement,
    operation_id: operationId("report-edit"),
  });
  toast(mutationMessage(result, "报告条目已修改。"));
  renderLiveReports(await api("workspace/reports"));
}

async function removeReportItem(button) {
  if (!requireSecondClick(button, "再次点击确认删除", "请再次点击确认删除这条报告内容。")) return;
  const result = await apiMutation(`workspace/reports/${encodeURIComponent(button.dataset.reportRef)}/commands`, "POST", {
    command_type: "delete_item",
    expected_version: Number(button.dataset.version),
    item_ref: button.dataset.itemRef,
    operation_id: operationId("report-delete"),
  });
  toast(mutationMessage(result, "报告条目已删除。"));
  renderLiveReports(await api("workspace/reports"));
}

async function submitReport(button) {
  if (!requireSecondClick(button, "再次点击确认提交", "请再次点击确认提交；提交后将不能继续修改。")) return;
  const result = await apiMutation(`workspace/reports/${encodeURIComponent(button.dataset.reportRef)}/commands`, "POST", {
    command_type: "submit_report",
    expected_version: Number(button.dataset.version),
    operation_id: operationId("report-submit"),
  });
  toast(mutationMessage(result, "报告已提交。"));
  renderLiveReports(await api("workspace/reports"));
}

function openDrawer(html) {
  drawerContent.innerHTML = html;
  drawer.classList.add("open");
  drawerBackdrop.classList.add("open");
}

function closeDrawer() {
  drawer.classList.remove("open");
  drawerBackdrop.classList.remove("open");
}

function toast(message) {
  const element = document.querySelector("#toast");
  element.textContent = message;
  element.classList.add("show");
  setTimeout(() => element.classList.remove("show"), 1800);
}

function openInlineEditor(button) {
  const parent = button.closest("li, .timeline-item");
  parent?.querySelector(".inline-actions")?.classList.add("hidden");
  const form = parent?.querySelector(".inline-edit-form");
  form?.classList.remove("hidden");
  form?.querySelector("input")?.focus();
}

function closeInlineEditor(button) {
  const parent = button.closest("li, .timeline-item");
  parent?.querySelector(".inline-edit-form")?.classList.add("hidden");
  parent?.querySelector(".inline-actions")?.classList.remove("hidden");
}

function requireSecondClick(button, confirmationText, helpText) {
  if (button.dataset.confirmReady === "true") {
    delete button.dataset.confirmReady;
    return true;
  }
  const originalText = button.textContent;
  button.dataset.confirmReady = "true";
  button.textContent = confirmationText;
  toast(helpText);
  setTimeout(() => {
    if (button.isConnected && button.dataset.confirmReady === "true") {
      delete button.dataset.confirmReady;
      button.textContent = originalText;
    }
  }, 5000);
  return false;
}

loginForm.addEventListener("submit", async event => {
  event.preventDefault();
  state.token = document.querySelector("#token-input").value.trim();
  loginError.textContent = "";
  sessionStorage.setItem("legalOpsCredential", state.token);
  await bootstrap();
});

if (window.location.port === "8765") demoLoginButton.classList.remove("hidden");
demoLoginButton.addEventListener("click", async () => {
  state.token = "codex-local-legal-ops";
  loginError.textContent = "";
  sessionStorage.setItem("legalOpsCredential", state.token);
  await bootstrap();
});

navigation.addEventListener("click", event => {
  const button = event.target.closest("[data-page]");
  if (!button) return;
  state.page = button.dataset.page;
  if (state.page === "cases") state.caseFilters = {};
  loadPage();
});

content.addEventListener("click", event => {
  const overviewRoute = event.target.closest("[data-overview-route]")?.dataset.overviewRoute;
  if (overviewRoute) {
    const [page, filter] = overviewRoute.split(":");
    state.page = page;
    state.caseFilters = page === "cases" ? (
      filter === "plaintiff_case" || filter === "defendant_case" ? {case_type: filter}
      : filter === "missing_progress" ? {progress_status: "missing"}
      : {}
    ) : {};
    return loadPage();
  }
  const liveCasePage = event.target.closest("[data-live-case-page]");
  if (liveCasePage && !liveCasePage.disabled) return loadLiveCases(Number(liveCasePage.dataset.liveCasePage));
  const reportEdit = event.target.closest("[data-start-report-edit]");
  if (reportEdit) return openInlineEditor(reportEdit);
  const cancelInlineEdit = event.target.closest("[data-cancel-inline-edit]");
  if (cancelInlineEdit) return closeInlineEditor(cancelInlineEdit);
  const reportDelete = event.target.closest("[data-report-delete]");
  if (reportDelete) return removeReportItem(reportDelete).catch(error => toast(error.message));
  const reportSubmit = event.target.closest("[data-report-submit]");
  if (reportSubmit) return submitReport(reportSubmit).catch(error => toast(error.message));
  const directPage = event.target.closest("[data-page]");
  if (directPage) { state.page = directPage.dataset.page; return loadPage(); }
  const applyBulkTarget = event.target.closest("[data-apply-followup-bulk]");
  if (applyBulkTarget) return applyBulkFollowup().catch(error => toast(error.message));
  const livePartyTarget = event.target.closest("[data-live-party]");
  if (livePartyTarget) return showLiveParty(livePartyTarget.dataset.liveParty);
  const exportTarget = event.target.closest("[data-export]");
  if (exportTarget) {
    const [period, fileFormat] = exportTarget.dataset.export.split(":");
    return downloadExport(period, fileFormat).catch(error => toast(error.message));
  }
  const pageRouteTarget = event.target.closest("[data-page-route]");
  if (pageRouteTarget) {
    state.page = pageRouteTarget.dataset.pageRoute;
    if (pageRouteTarget.dataset.team) {
      state.teamId = pageRouteTarget.dataset.team;
      teamFilter.value = state.teamId;
    }
    return loadPage();
  }
  const phase2CaseTarget = event.target.closest("[data-phase2-case]");
  if (phase2CaseTarget) return showPhase2Case(phase2CaseTarget.dataset.phase2Case);
  const caseTarget = event.target.closest("[data-case]");
  if (caseTarget) return showCase(caseTarget.dataset.case);
  const reportTarget = event.target.closest("[data-report]");
  if (reportTarget) return showReport(reportTarget.dataset.period, reportTarget.dataset.report);
  const route = event.target.closest("[data-route]")?.dataset.route;
  if (route) {
    const [page, queryString] = route.split("?");
    if (["performance", "work", "travel", "teams", "forest"].includes(page)) {
      state.page = page;
      return loadPage();
    }
    state.page = "forest";
    if (state.page === "forest") return loadCases(Object.fromEntries(new URLSearchParams(queryString || "")));
    return loadPage();
  }
  const team = event.target.closest("[data-team-cases]")?.dataset.teamCases;
  if (team) {
    state.teamId = team; teamFilter.value = team; state.page = "forest"; loadCases({ teamId: team });
  }
});

content.addEventListener("submit", event => {
  const reportEditForm = event.target.closest("[data-report-edit-form]");
  if (reportEditForm) {
    event.preventDefault();
    return editReportItem(reportEditForm).catch(error => toast(error.message));
  }
  const reportForm = event.target.closest("[data-report-append]");
  if (reportForm) {
    event.preventDefault();
    return appendReportItem(reportForm).catch(error => toast(error.message));
  }
  const form = event.target.closest("#followup-bulk-form");
  if (!form) return;
  event.preventDefault();
  previewBulkFollowup(form).catch(error => toast(error.message));
});

drawerContent.addEventListener("click", event => {
  const caseSection = event.target.closest("[data-case-section-target]");
  if (caseSection) return activateCaseSection(caseSection);
  const progressEdit = event.target.closest("[data-start-case-progress-edit]");
  if (progressEdit) return openInlineEditor(progressEdit);
  const cancelInlineEdit = event.target.closest("[data-cancel-inline-edit]");
  if (cancelInlineEdit) return closeInlineEditor(cancelInlineEdit);
  const progressDelete = event.target.closest("[data-delete-case-progress]");
  if (progressDelete) return removeCaseProgress(progressDelete).catch(error => toast(error.message));
  const cancelFollowup = event.target.closest("[data-cancel-followup]");
  if (cancelFollowup) return cancelCaseFollowup(cancelFollowup).catch(error => toast(error.message));
  const triggerNow = event.target.closest("[data-trigger-followup-now]");
  if (triggerNow) return triggerCaseFollowupNow(triggerNow).catch(error => toast(error.message));
  const phase2CaseTarget = event.target.closest("[data-phase2-case]");
  if (phase2CaseTarget) return showPhase2Case(phase2CaseTarget.dataset.phase2Case);
  const caseTarget = event.target.closest("[data-case]");
  if (caseTarget) showCase(caseTarget.dataset.case);
});

drawerContent.addEventListener("submit", event => {
  const progressEditForm = event.target.closest("[data-edit-case-progress-form]");
  if (progressEditForm) {
    event.preventDefault();
    return editCaseProgress(progressEditForm).catch(error => toast(error.message));
  }
  const progressForm = event.target.closest("[data-add-case-progress]");
  if (progressForm) {
    event.preventDefault();
    return addCaseProgress(progressForm).catch(error => toast(error.message));
  }
  const form = event.target.closest(".followup-policy-form");
  if (!form) return;
  event.preventDefault();
  saveCaseFollowupPolicy(form).catch(error => toast(error.message));
});

teamFilter.addEventListener("change", () => { state.teamId = teamFilter.value; loadPage(); });
document.querySelector("#refresh-button").addEventListener("click", () => { toast("数据已刷新"); loadPage(); });
document.querySelector("#identity-button").addEventListener("click", () => {
  if (state.shell?.mode === "sandbox_live") {
    sessionStorage.removeItem("legalOpsCredential");
    state.token = "";
    state.shell = null;
    document.querySelector("#token-input").value = "";
    loginError.textContent = "";
    loginModal.classList.remove("hidden");
    return;
  }
  state.page = "admin";
  loadPage();
});
document.querySelector("#drawer-close").addEventListener("click", closeDrawer);
drawerBackdrop.addEventListener("click", closeDrawer);
document.addEventListener("keydown", event => { if (event.key === "Escape") closeDrawer(); });

renderNavigation();
bootstrap();
