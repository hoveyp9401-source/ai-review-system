# Legal Ops 真实性与产品化只读审计（2026-07-13）

> 本文保留改造前只读基线。文中 `NO_GO`、单管理员、报告/团队缺失等结论已被同日实施闭环取代；最终状态与裁决见 `LEGAL_OPS_TWO_USER_UI_ACCEPTANCE_20260713.md`。

## 审计范围

- 本地 `127.0.0.1:8765/legal-ops/`：`app.legal_ops.dev_app`，当前运行在 `demo` Fixture 模式。
- 服务器 `127.0.0.1:8000/legal-ops/`（通过本地 8766 SSH 隧道只读访问）：`sandbox_live`，读取 tenant `sandbox-agent2-phase2-20260711` 的 PostgreSQL。
- 前端：`app/legal_ops/static/index.html`、`app.js`、`styles.css`。
- 后端：`app/legal_ops/api.py`、`auth.py`、`phase2_read.py`、`repository.py`、`service.py`。
- 数据：当前服务器 PostgreSQL、Agent2 两名 identity binding、80 件权限内真实来源灰测案件。
- 本阶段未修改业务代码、未写数据库、未创建 Fixture。

## 结论

当前 Legal Ops 同时存在两套体验：

1. 本地 8765 是完整但全为 Fixture 的演示中台；
2. 服务器是 PostgreSQL 实时读模型，但仅有“总览、主体、案件森林、出差、审计”，报告和团队入口缺失，并直接暴露 UUID、provider message ID、UTC 时间、`planned`、`accepted`、`plaintiff`、`adjudicated` 等内部字段。

因此当前状态为 `NO_GO` 基线：不能把本地页面当真实中台，也不能把服务器实时读模型当已产品化中台。

## 已核验服务器事实

### 身份与权限

- identity binding：2 条，庞浩、刘聪；每人 `allowed_case_ids=40`。
- 两人都映射到 Agent2 team `sandbox-agent2-team`。
-正式 `users/teams` 中两人属于同一 Team，但 Team 名仍是 `Team 01`，部门名仍是 `AI Department`。
- 当前 live 登录只支持一个 `tenant_admin` token，principal user 是 `sandbox-admin`；尚未形成庞浩、刘聪各自独立的中台身份。
- live 读 API 目前只做 tenant fence；`phase2`、单案、主体详情没有按 principal 的 40 件案件权限过滤。当前单一管理员可以看到 80 件，不能证明用户隔离。

### 案件

- `agent2_cases=83`：80 件 `real_case_workbook`，3 件 `sandbox_fixture` 且 `display_hidden=true`。
- 默认 live 读模型显示 80 件，未把 3 件旧 Fixture 混入列表。
- `plaintiff_case=40`，`defendant_case=40`，旧 `litigation=3`（隐藏）。
- 庞浩 43 件包含 3 件隐藏 Fixture，实际权限集合 40；刘聪 40 件。
- 原告阶段状态：`intended_filing / litigation / enforcement / closed`，各真实来源 10 件。
- 被告阶段状态：`accepted / hearing / adjudicated / performance / closed`，各真实来源 8 件。
- 当前案件没有风险等级、法院、案由、开庭日期等独立结构化字段；不得在新页面伪造。
- `CaseLifecycleState=0`。当前没有真实的阶段/节点状态表记录，页面只能把 `Agent2Case.status` 作为导入阶段展示，并标明来源。
- `CaseProgress=8`，其中 6 条软删除；有效进展只有 2 条。大多数案件没有最新进展和下一步计划。

### 报告

- 当前两名用户共有日报 39 份：24 已完成、10 收集中、5 待确认。
- 庞浩最新日报日期 2026-07-12，状态 collecting；刘聪最新日报日期 2026-07-10，状态 completed。
- 周报 1 份、月报 1 份，均属于庞浩，来源 channel 为 `server_natural_language_greytest`。
- 服务器 live 导航没有报告入口，`/api/reports/*` 在 live mode 强制 404；真实报告尚未接入中台。

### 出差、通知与追问

- TravelIntent 5 条：3 条来自真实钉钉 stream，2 条 `server_acceptance_smoke`。
- live 默认列表把 Smoke 与真实用户消息混在一起；必须默认排除 Smoke 或单独放到验收证据视图。
- 协同候选 1 条，状态 `accepted`，两人均有 accept 回复记录。
- Notification 3 条，均为 `sent` 且有 provider message ID；页面把 provider ID、用户 UUID 和原始状态直接暴露。
- CaseFollowupTask=0，CaseFollowupPending=0，CaseLifecycleState=0；页面中“主动追问”只能展示 policy，不得暗示已有追问闭环。
- CaseFollowupPolicy=80，全部 `event_only / bulk_assignment`。

## 关键问题

### P0

1. **用户权限未闭环**：live principal 是单一管理员，读 API 只按 tenant 过滤；庞浩、刘聪无法各自登录并只看自己的 40 件案件。
2. **本地入口误导**：localhost 前端硬编码 token `codex-local-legal-ops`，8765 固定进入 Fixture；同一页面通过 8766 访问服务器时也先自动提交错误 token，出现 `invalid sandbox credential`。
3. **报告/团队缺失**：真实日报、周报、月报和人员团队数据已在 PostgreSQL，但 live API/导航没有接入。

### P1

1. “SANDBOX FIXTURES”静态标题在 live 模式仍显示。
2. live 页面暴露 tenant、UUID、source_message_id、provider message ID、receipt ID、version、UTC ISO 字符串和内部枚举。
3. 80 件案件全部纵向展开，无分页、无搜索、无原告/被告顶部切换；“案件森林”以 Party role 作为主结构。
4. live 案件标签显示的是对手方 role（如 `plaintiff · 李嫣红`），不是“我方为原告/被告”的案件类型，容易产生反向理解。
5. 软删除进展仍在森林枝条直接显示，虽标“已软删除”但不应进入默认最新进展。
6. live 出差把 Smoke 与真实用户数据混列，状态和回复 JSON 未中文化。
7. live 单案详情只显示追问策略、主体和进展；基本信息、阶段时间线、下一步、开庭、日报投影、审计未形成完整工作台。
8. 页面大量按钮没有 loading/error/版本冲突的稳定页面态，只使用短暂 toast；刷新按钮文案仍是“只读 BFF”。

## 当前截图证据

截图已保存在 `artifacts/legal-ops-ui-audit/before/`：

1. `01-command-center-fixture-dashboard.png`：本地 Fixture 首页与假指标。
2. `02-reports-fixture-table.png`：演示人员和 24 份 Fixture 日报。
3. `03-case-forest-fixture-enums.png`：12 件 Fixture 案件和原始英文枚举。
4. `04-fixture-case-workspace-lifecycle.png`：Fixture 单案工作台和伪完整生命周期。
5. `05-performance-fixture-tabs-and-exports.png`：演示绩效、目标收集、数据维护与导出入口。
6. `06-team-fixture-members.png`：演示团队和演示成员。
7. `07-travel-fixture-collaboration.png`：Fixture 出差协同。
8. `08-live-overview-postgres-technical-fields.png`：真实 PostgreSQL 总览但暴露技术字段。
9. `09-live-case-forest-80-unpaginated.png`：80 件真实来源案件无分页的森林。
10. `10-live-case-detail-owner-id-and-actions.png`：单案详情暴露 UUID/枚举，追问按钮已接正式 API。
11. `11-live-travel-raw-status-and-message-ids.png`：真实出差混入 Smoke、原始状态和消息 ID。

## 审计边界

- 截图可以证明页面结构、文案和可见状态，不能单独证明数据库事务与权限；数据库事实来自本轮服务器 ORM 只读查询。
- 本轮没有点击任何会产生写入的按钮。
- 没有把 provider `sent` 推断为送达；当前仅能证明 outbox 为 sent 且存在 provider message ID。

## 实施后差异摘要

- 庞浩、刘聪已使用独立登录凭证进入同一 Sandbox tenant，后端分别按 principal 的 40 件 `allowed_case_ids` 裁剪案件。
- 默认 live 导航已收敛为工作总览、报告中心、案件工作台、出差协同和团队；审计入口仅对有权角色显示。
- 报告中心已接 DailyReport 与 PeriodicReport 正式命令入口；案件进展已接 typed command、version、idempotency、receipt、audit。
- 旧 Fixture、隐藏案件、provider message ID、UUID、receipt、source message 和内部枚举不进入两用户默认业务视图。
- 单案生命周期、进展、下一步计划、当事人、追问状态与脱敏操作记录均来自 PostgreSQL；缺失字段明确显示“暂未记录/暂未评估”。
- 本文列出的 P0 项均已关闭；剩余真实用户验收不足与历史正文质量问题记录在最终问题台账。
