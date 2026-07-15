# Legal Ops 两用户真实 UI 验收报告（2026-07-13）

## 最终裁决

`READY_FOR_TWO_USER_UI_CANARY`

庞浩、刘聪已经可以使用各自身份进入真实 Legal Ops 中台，并在后端权限范围内查看各自 40 件案件、本人报告、出差和团队信息。庞浩身份下的案件进展与日报条目增改删已完成真实浏览器、API、PostgreSQL、receipt/audit 和刷新回读闭环。默认 live 页面没有 Fixture、前端假数字、内部 ID 或原始英文状态。

本轮不裁决 `TWO_USER_UI_CANARY_VALIDATED`：刘聪本人尚未完成写操作闭环，两名用户也尚未亲自完成一次报告提交。本轮代理操作不能冒充真人验收。

## 交付结果

| 交付项 | 结果 | 证据 |
|---|---|---|
| 假数据来源审计 | 完成，保留改造前 `NO_GO` 基线 | `LEGAL_OPS_REALITY_AUDIT_20260713.md` |
| 页面—字段—数据源矩阵 | 完成并补充实施后状态 | `LEGAL_OPS_PAGE_DATA_SOURCE_MATRIX_20260713.md` |
| 页面—按钮—能力矩阵 | 完成并补充实际闭环状态 | `LEGAL_OPS_BUTTON_CAPABILITY_MATRIX_20260713.md` |
| 无效页面/指标清理 | demo 指标不进入 live；live 收敛为 5 个业务入口 | 本报告“信息架构” |
| 指标口径 | 权限案件、原告、被告、阶段、进展、报告、出差均可追溯 | `LEGAL_OPS_INFORMATION_ARCHITECTURE_20260713.md` |
| 中文词典 | 案件、报告、通知、来源、追问闭集 | `LEGAL_OPS_CHINESE_BUSINESS_DICTIONARY.md` |
| 案件工作台/生命周期 | 案件卡片、分页、筛选、单案生命周期和真实区块导航 | 浏览器截图 14、20、22 |
| API 与前端改造 | scoped read DTO + typed write endpoints + inline actions | 本报告“修改清单” |
| 权限验证 | 两独立 principal，各 40 件；详情越权 fail-closed | 自动化测试与双浏览器读验证 |
| 自动化与服务器测试 | 本地 77 + 20；部署代码服务器 44 | 本报告“测试结果” |
| 真实服务器 E2E | 庞浩写闭环完成；刘聪读隔离完成 | 本报告“E2E” |
| 回滚 | live kill switch + 增量备份 +数据边界 | `LEGAL_OPS_TWO_USER_UI_ROLLBACK_20260713.md` |
| 问题台账 | 12 项关闭、6 项真实未满足 | `LEGAL_OPS_TWO_USER_UI_ISSUES_20260713.md` |
| 机器裁决 | 完成 | `artifacts/legal-ops-ui-audit/verdict.json` |

## 信息架构与真实数据

live 页面保留：工作总览、报告中心、案件工作台、出差协同、团队。管理审计只对有权角色开放；主体关系进入单案辅助区块，不再占顶层导航。

| 身份 | 案件 | 原告 | 被告 | 报告 | 出差 | 数据边界 |
|---|---:|---:|---:|---:|---:|---|
| 庞浩 | 40 | 20 | 20 | 23 日报 + 1 周报 + 1 月报 | 3 | 本人 + `allowed_case_ids` |
| 刘聪 | 40 | 20 | 20 | 17 日报 | 2 | 本人 + `allowed_case_ids` |
| 团队汇总 | 80 | 40 | 40 | 按成员真实记录聚合 | 5 | 当前 Sandbox team |

旧 `sandbox_fixture` 隐藏案件、演示成员、随机指标、provider message ID、receipt/source message/UUID 均不进入两用户默认业务视图。本地 8765 的 Fixture 只保留为显式演示模式，与 8766 live 登录及统计隔离。

没有可靠结构化来源的法院、案由、开庭日期和风险等级不推导，统一显示“暂未记录/暂未评估”。

## 核心真实交互

### 案件进展

目标案件：`BGGL-2512-0023`（名称见截图，内部 case ID 不在 UI 展示）。

```text
新增进展
→ 刷新后仍存在
→ 行内修改
→ 刷新后仍为修改值
→ 二次确认删除
→ 刷新后不再显示
→ 由该进展创建的 lifecycle 同事务删除
```

对应 committed receipts：

- 新增：`1743c63c-3844-59ea-8b3e-0b12d53af706`
- 修改：`8632c663-4a1a-5f55-83d8-8173deeeca20`
- 删除：`1ec5654a-6d65-5f90-863a-7118d94de160`

删除 receipt 中的 `lifecycle_reversal.action=deleted_created_state`；最终案件无有效验收进展、无 lifecycle、下一步计划为“暂未记录”。单案页面只显示中文“新增/修改/删除案件进展 · 已写入”，不下发内部证据 ID。

### 日报条目

庞浩 2026-07-13 日报：

```text
新增临时条目
→ 整页刷新并重新进入报告中心，仍存在
→ 行内修改
→ 再次刷新，修改值仍存在
→ 二次确认删除
→ 再次刷新，条目消失
```

对应 committed receipts：

- 新增：`bba88ad9-af0f-52f1-bfdb-c08f2f8f4e74`
- 修改：`71290b11-75d0-5f71-93cc-3b8446d9d896`
- 删除：`2d3757fc-46c4-58c5-b96e-3cdf0b0116bb`

三张 receipt 均为 `executed / authorized / actual_write=true`；PostgreSQL `temporary_content_remaining=[]`。页面来源显示“法务业务中台”。证据见 `.codex_tmp/server_probe_report_ui_e2e.json`。

### 双用户隔离

- 庞浩登录：40 件案件、20 原告、20 被告、25 份报告、3 条出差。
- 刘聪登录：40 件案件、20 原告、20 被告、17 份日报、2 条出差；报告列表只显示刘聪。
- case owner 的 scope 从 active IdentityBinding 的 `permission_scope_json` 读取，不信任 credential 中携带的案件列表。
- 详情 URL 每次重验 allowed case；跨 scope 返回 not found，避免枚举隐藏案件。
- 报告写接口只允许 owner；案件写接口校验 tenant、actor、case permission、version 和 idempotency。

## API 修改清单

- `GET /api/workspace/cases`：scoped 筛选、分页和中文 DTO。
- `GET /api/workspace/cases/{case_id}`：单案、生命周期、有效进展、追问、脱敏审计与日报投影。
- `POST/PUT/DELETE /api/workspace/cases/{case_id}/progress...`：正式 CaseProgress typed command。
- `GET /api/workspace/reports`：日报、周报、月报统一读取。
- `POST /api/workspace/reports/{report_ref}/commands`：新增、修改、删除、提交走 Report Executor。
- `GET /api/workspace/team`：IdentityBinding 与真实业务聚合。
- `GET /api/workspace/travel`：出差、候选和通知中文事实投影。
- `GET /api/phase2`：按 principal scope 的真实总览与管理员证据入口。

## 修改文件清单

核心后端：

- `app/legal_ops/api.py`
- `app/legal_ops/auth.py`
- `app/legal_ops/business_labels.py`
- `app/legal_ops/live_workspace.py`
- `app/legal_ops/phase2_read.py`
- `app/legal_ops/write_service.py`
- `app/agent2/business/sql_executor.py`

核心前端：

- `app/legal_ops/static/index.html`
- `app/legal_ops/static/app.js`
- `app/legal_ops/static/styles.css`

专项测试：

- `tests/test_legal_ops_product_ui.py`
- `tests/test_legal_ops_phase2_read.py`
- `tests/test_legal_ops_write_service.py`
- `tests/test_legal_ops_e2e.py`
- `tests/test_legal_ops_followup_bulk.py`
- `tests/test_legal_ops_sandbox.py`
- `tests/test_agent2_business_sql_executor.py`

## 数据库与映射调整

- 本轮没有为 UI 新建重复业务表，也没有生成新的 Fixture。
- 复用现有 Agent2Case、IdentityBinding、DailyReport、PeriodicReport、CaseProgress、LifecycleState、Party、Travel、Notification、Followup 和 CaseReportProjection。
- 庞浩、刘聪 principal 使用 server-side credential directory；秘密不写入报告或前端。
- 一次历史污染修复通过正式 receipt/audit 删除孤儿 lifecycle；没有用无审计手工 SQL 冒充 E2E。

## 测试结果

| 层级 | 结果 |
|---|---|
| Legal Ops 本地专项 | 77 passed |
| Case SQL Executor | 20 passed |
| 前端语法 | `node --check` passed |
| 部署代码服务器专项 | 44 passed |
| Agent2 前序聚焦回归 | 208 passed |
| 全仓前序回归 | 1633 passed、1 skipped、142 legacy failures；分类为 136 旧协议、6 无关、0 当前 Runtime 直接回归 |

部署服务器与本地最终哈希一致：

| 文件 | SHA-256 |
|---|---|
| `phase2_read.py` | `abe05758f647077702bf86d986e4b4eb9486b7353a78c86fc426e55733464856` |
| `live_workspace.py` | `83c204bf155a1727e1029b1279dc58153a00e0d3abbff976e182e90c0caedb4d` |
| `app.js` | `ec9df0a0e55a35e98c85e9cb7e252eef2e9be122f658bcf297ca40405f40a8a4` |
| `styles.css` | `3c116247f55f018c379b42e8911fb1bd91f9e70ad2ccfc8999448c31161fb780` |
| `index.html` | `0c1b90730803617b20593cae826b0e598e7691a5039f61cd7a65825b1cb33139` |
| `sql_executor.py` | `c1a4a4626ed5e93aaf792eba15b0fbeca0c68df07497b7ca9feea8584f58062b` |

API、Stream、Scheduler 均为 `active`，API health 为 `{"status":"ok"}`。

## 截图证据

改造前：`artifacts/legal-ops-ui-audit/before/01` 至 `11`。

改造后重点：

- `after-12-pang-overview.png`
- `after-13-reports.png`
- `after-14-cases.png`
- `after-15-travel.png`
- `after/18-pang-report-center-final.png`
- `after/19-liu-report-scope.png`
- `after/20-pang-case-navigation-final.png`
- `after/22-pang-case-report-projection.png`

## 未完成与升级条件

升级为 `TWO_USER_UI_CANARY_VALIDATED` 前必须：

1. 刘聪本人完成一次案件或报告的增改删刷新闭环；
2. 庞浩、刘聪至少各自完成一次本人选择的报告提交；
3. 观察期内无跨用户、假成功、重复写入或状态越级表达；
4. 首条真实 CaseReportProjection 产生后复核 active/removed 展示；
5. 管理员扩围前完成管理审计页产品化验收。

在此之前，只允许当前 tenant、庞浩和刘聪继续灰测，不扩大用户范围。
