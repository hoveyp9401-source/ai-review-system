# Legal Ops 页面—字段—数据源矩阵（2026-07-13）

| 页面/区块 | 当前字段/指标 | 当前数据来源 | 当前分类 | 目标真实数据源 | API | 权限规则 | 当前交互 | 目标交互 |
|---|---|---|---|---|---|---|---|---|
| 本地作战指挥中心 | 82%、75%、重点案件、行动中心 | `phase0_manifest.json` + `LegalOpsReadService` 固定数组/公式 | `seed_fixture` | 删除；改为真实案件、报告、追问、出差异常指标 | 新 live overview | tenant + principal case/report scope | 卡片跳演示页 | 指标下钻真实记录 |
| 本地工作与日报 | 24 份、4 人、演示成员内容 | Phase0 JSON | `seed_fixture` | `daily_reports` + `agent2_periodic_reports` | 新 `/api/live/reports` | 用户仅本人，管理员按团队 | 只读演示 | 查/增/改/删/提交走 Report Executor |
| 本地绩效与报告 | 12 项指标、目标收集、人工维护 | Phase0 JSON/固定文案 | `seed_fixture` | 当前无正式口径的区块删除；真实周/月报保留 | 新 `/api/live/reports` | 报告 owner/团队管理员 | 演示 tab/导出 | 未接能力隐藏；真实报告可编辑提交 |
| 本地团队工作台 | 3 个演示团队、4 个演示成员 | Phase0 JSON | `seed_fixture` | identity binding + users + server-side team mapping | 新 `/api/live/teams` | tenant/team scope | 只读演示 | 团队/人员可下钻真实案件和报告 |
| 本地案件森林 | 12 件合成案件、假风险/时间线 | Phase0 JSON | `seed_fixture` | `agent2_cases`、Party、有效 CaseProgress、LifecycleState | 新 `/api/live/cases` | principal allowed_case_ids | 森林缩放/筛选 | 原告/被告列表、分页、筛选、单案详情 |
| 本地出差协同 | 3 条演示出差 | Phase0 JSON | `seed_fixture` | TravelIntent/Candidate/Outbox | 新 `/api/live/travel` | 当前用户/管理员 scope | 只读演示 | 状态中文化、来源隔离、详情下钻 |
| live 总览 | 78 主体、80 案件、8 进展、5 出差、3 通知、100 回执 | PostgreSQL `load_phase2_read_model` | `real_postgresql`（含 Smoke） | 权限化聚合，不显示技术计数/ID | `/api/phase2` → 新 overview | 当前仅 tenant；需 principal scope | 只读 | 卡片下钻已过滤记录 |
| live 最新进展 | summary、UTC、version、origin | CaseProgress | `sandbox_persisted` | 仅有效未删除进展；中文时间/来源 | 新 case summary API | case permission | 只读 | 进入单案进展；管理员可正式 CRUD |
| live 通知状态 | message_type、UUID、provider ID | NotificationOutbox | `real_postgresql` | 中文状态；隐藏 provider ID/UUID | 新 notification summary | recipient/admin | 只读 | 只显示排队/平台受理/等待回复等事实 |
| live 主体知识库 | PartyEntity 等 8 表 | PostgreSQL | `real_source_sandbox` | 保留，中文化，按有权案件过滤关系 | `/api/phase2/parties*` | 当前仅 tenant；需 case-scope | 搜索/详情 | 规范名、别名、标识、角色、关系、来源 |
| live 案件森林 | 80 件纵向全展开 | Agent2Case + PartyCaseRole + CaseProgress | `real_source_sandbox` | 原告/被告案件总览 + 分页卡片 | 新 `/api/live/cases` | 当前仅 tenant；需 allowed_case_ids | 80 件无分页 | 搜索、分类、阶段、负责人、分页 |
| live 单案详情 | owner UUID、status、policy、主体、进展 | PostgreSQL | `real_postgresql` | 中文负责人、基本信息、阶段/节点、进展、计划、追问、审计 | 新 `/api/live/cases/{id}` | 当前仅 tenant；需 case permission | drawer；policy 可写 | 独立详情工作台，所有写入回读 |
| live 出差 | 5 Intent、1 Candidate、3 Outbox | PostgreSQL | 混合 `real_user_message` + `server_acceptance_smoke` | 默认只展示真实用户记录，Smoke 进审计页 | 新 `/api/live/travel` | user/participant/admin | 原始表格 | 中文本地时间、状态闭集、通知事实 |
| live 审计 | receipt/audit/source_message/provider ID | PostgreSQL | `real_postgresql` | 管理员专用，业务页隐藏技术字段 | `/api/phase2` 或新 audit API | tenant_admin only | 全量原始表 | 分页、筛选、脱敏、证据下载 |
| live 报告 | 当前不存在 | API 404 | `not_implemented` | DailyReport + PeriodicReport 统一视图 | 新 `/api/live/reports*` | owner/team/admin | 无入口 | 查询、编辑、删除、提交、回读 |
| live 团队 | 当前不存在 | shell 返回空 teams/users | `not_implemented` | IdentityBinding + User/Team + TeamMapping | 新 `/api/live/teams*` | team/admin | 无入口 | 两名成员、案件数、报告提交状态 |

## 实施后页面—字段—数据源状态

| 页面 | 生产灰测数据源 | principal 范围 | Fixture 默认可见 | 最终状态 |
|---|---|---|---|---|
| 工作总览 | scoped Phase 2 read model + scoped case workspace | tenant + user + allowed cases | 否 | 40 件/人、原告 20、被告 20，可下钻 |
| 报告中心 | `daily_reports` + `agent2_periodic_reports` | 本人报告 | 否 | 日/周/月统一查询，正式命令增改删提交 |
| 案件工作台 | `agent2_cases` + Party + 有效 CaseProgress + LifecycleState + FollowupPolicy | 40 件 `allowed_case_ids` | 否 | 分类、阶段、进展筛选、分页、单案下钻 |
| 单案详情 | Case/Party/Progress/Lifecycle/Followup/Audit | 每次请求重验案件权限 | 否 | 生命周期、进展计划、当事人、追问、操作记录 |
| 出差协同 | TravelIntent/Candidate/Notification | 当前用户/参与人 | 否 | 中文事实状态，隐藏 transport ID |
| 团队 | IdentityBinding + server-side team mapping + scoped aggregates | 当前 Sandbox team | 否 | 两名成员和真实案件/报告/出差汇总 |
| 管理审计 | receipt/audit/read model | 角色控制 | 否 | 不进入庞浩、刘聪默认导航 |

分类结论：默认两用户页面为 `real_postgresql` 或 `real_source_sandbox`；本地 8765 演示入口仍是显式 `seed_fixture`，与 live 登录及统计隔离。
