# Agent2 案件生命周期主动协同验收报告（2026-07-13）

## 裁决

`SHADOW_ONLY`

允许当前 Sandbox tenant、庞浩、刘聪及其权限集合内 80 件案件继续生成和审核追问/投影决策；不允许开启主动消息发送或自动日报投影，不扩大用户范围。

主要阻断不是代码测试失败，而是 Blind Gate 和真人证据不足：密封评分 47 turn 中 21 项不一致，且 `independent_metrics_available=false`；庞浩、刘聪均未在本版本完成真实主动追问回复闭环。

## 已实现

- 统一 `OperationOutcome`，Reply Composer 只解释 Outcome；最终回复所消费的 Outcome 以及通知 provider acceptance 持久化到 `agent2_operation_outcomes`。
- 持久化 Case Lifecycle State、CaseFollowupPolicy、Task、Pending、Task Ledger、ReportProjectionRequest 和 CaseReportProjection。
- 支持 daily / weekly / 15 days / calendar monthly / custom cadence、7/3/1 天开庭提醒、庭后询问、committed stage transition、allowlisted node transition 和人工立即追问。
- Scheduler 仅扫描两名用户 identity binding 的 40+40 Case 集合，不扫描 tenant 中另外 3 条 Case。
- 发送、回复和 Task 生命周期分离；provider acceptance 必须有 provider message ID，且不表达为送达。
- provider receipt 先独立提交，再幂等推进 Pending；Pending 推进失败由 reconciliation 重试，不会抹去已外发证据。
- 用户/案件日配额通过 PostgreSQL advisory transaction lock 串行化；提醒使用独立次数、间隔和幂等键。
- Pending 回复重新校验 tenant、user、conversation、权限、Pending/Task/当前 Case version、过期和消费状态。
- Case 写入为主，Report Projection 为持久化派生请求；无日报时 `report_not_found`，不会隐式创建日报。
- 投影正文固定使用用户原文；生命周期/动作字段只有在用户原文中存在时才进入 typed command 或投影策略。时间锚由原文动作所在分句确定性复核，缺失 evidence 不再自动伪造整段 span；未来动作被模型误标为今日完成时 fail-closed。
- 日报投影支持 confirmation、精确删除、移动和改写，且不删除/改写 Case Progress。
- Legal Ops 支持单案/批量策略、立即追问、取消未发送 Task、状态/历史和指标查询；写操作带 version、idempotency、receipt 和 audit。

## 数据模型与 migration

Migration：`scripts/create_agent2_case_lifecycle_followup.sql`。

新增表：

- `agent2_case_lifecycle_states`
- `agent2_case_followup_policies`
- `agent2_case_followup_tasks`
- `agent2_case_followup_pendings`
- `agent2_task_ledger`
- `agent2_report_projection_requests`
- `agent2_case_report_projections`
- `agent2_operation_outcomes`

## 测试与评测

### 本地

- Agent2 / Legal Ops 定向：`1079 passed, 1 skipped`。
- 全仓：`1585 passed, 142 failed, 1 skipped`；可复现 JUnit：`artifacts/agent2-case-followup-final/regression/pytest-final-20260713.xml`。
- 142 项与改造前基线相同：136 项 deprecated legacy DailyReportService contract，6 项禁止进入 Runtime 依赖图的旧 `app.agent_core`；`direct_runtime_impact=0`、`real_regression=0`。
- Follow-up/Projection 专项多轮定向集均全绿。
- 对抗/Blind 架构测试：`76 passed`。
- 确定性对抗语料：126 cases，blind/sealed hash 已保存。

### Blind 结果

- 20 cases / 47 turns 的密封运行已完成。
- actual artifact hash：`7dd438fe4a97471e8b625ad5fb888a825d969b988106657ce326d3256f7f810c`。
- 21 mismatch；`independent_metrics_available=false`。
- 旧 replay 为诊断用途：unexpected write=0、legacy fallback=0、typed executor bypass=0，但 acceptance eligible=false，不能用来证明 Canary ready。

### 服务器真实 PostgreSQL

- Migration 成功，8 张表存在。
- opt-in PostgreSQL integration：`1 passed`。测试通过 typed `TriggerCaseFollowupNow` 生成 Task/receipt/audit 后回滚，数据库未留下证明数据。
- 原服务器专项：`62 passed`；末次时间锚、evidence、通知 Outcome 补偿修复部署后相关集：`62 passed`。
- 真实批量 API：preview=80，apply=80，failed=0；结果为 80 policy、80 receipt、80 audit、0 lifecycle task、0 lifecycle outbox。
- 首次真实批量写暴露 receipt/audit flush 顺序造成的 FK 问题；修复后重跑成功。

## 部署证据

- 服务器：`/home/ai_review_tunnel/ai-review-system`。
- 部署前备份：`/home/ai_review_tunnel/backups/agent2-case-followup-/code-before.tar.gz` 和 `env.backup`。
- 12 个核心文件及末次 5 个安全修复文件本地/服务器 SHA-256 一致。
- API、Stream、Scheduler 均为单实例 active；health=`{"status":"ok"}`；启动后 warning/error 日志为空。
- 当前开关：
  - `CASE_FOLLOWUP_ENABLED=true`
  - `CASE_FOLLOWUP_SEND_ENABLED=false`
  - `CASE_FOLLOWUP_REPORT_PROJECTION_ENABLED=false`
  - 原旧 Follow-up 发送开关也已暂时关闭。
  - tenant/user allowlist 恰为当前 tenant、庞浩、刘聪。
- 末次部署和 PostgreSQL 复验工件：`artifacts/agent2-case-followup-final/server-verification-20260713.md`。

## 新发现问题台账

1. `BLOCKER`：Blind 密封评分 21/47 mismatch，且尚无独立 Gold；不得开启 effect。
2. `BLOCKER`：两名真人尚未在本版本完成收到/回复主动追问、今日动作投影、未来计划投影、状态不投影、opt-out、撤销和报告恢复验收。
3. `BLOCKER`：两名用户均存在多个历史 conversation state，未配置经确认的 conversation map；系统按协议阻断，不猜最近会话。
4. `P1`：`duplicate_followups_prevented` 与 `repeat_message_deduplicated` 目前无法从持久化事件精确回算，指标 API 明确返回 evidence unavailable。
5. `P1`：没有可靠钉钉 delivery callback；最高只能标记 `accepted_by_provider`。
6. `P2`：服务器全量测试受 live 环境变量、缺少旧 acceptance artifacts/导出文件及服务器测试副本差异影响；专项和真实 PG 验证通过，不能把服务器全量环境失败写成全绿。
7. `P1`：当前单条 `CaseFactExtraction` 只有一个 `action_time_scope`；同一分句同时包含“今天已完成”和“明天计划”时会安全阻断，尚不能自动拆分并分别投影到两个日报区块。

## 独立双轴复核

- Standards Review：provider receipt 边界、当前 Case version、confirmation 同事务、并发配额、evidence 覆盖和 sent-without-outcome 补偿均已复核关闭；末次复核无新增 P0/P1。
- Spec Review：时间锚 grounding、缺失 evidence 阻断、生产投影 P0 回归、provider Outcome 持久化和限定 PostgreSQL/部署证据已关闭；Blind Gate、真人验收与混合时间动作拆分仍开放。
- 两轴共同允许的最高裁决均为 `SHADOW_ONLY`。

## 回滚

立即保持/设置 `CASE_FOLLOWUP_SEND_ENABLED=false` 和 `CASE_FOLLOWUP_REPORT_PROJECTION_ENABLED=false`；必要时再设置 `CASE_FOLLOWUP_ENABLED=false` 并重启 Scheduler。代码回滚使用部署前备份。新表保留为审计证据，不在事故时删除。详见 `docs/AGENT2_CASE_FOLLOWUP_ROLLBACK.md`。

## 下一 Gate

1. 对 Blind mismatch 做独立人工 adjudication，并把安全类 mismatch 降为 0。
2. 由用户确认庞浩、刘聪各自唯一的 conversation mapping。
3. 发送开关仍关闭时生成并人工审核真实 Shadow Task/问法/投影决定。
4. 再仅对两人开启发送；自动投影仍只开放高置信度。
5. 两人完成真实验收且无高危事故后，才可裁决 `TWO_USER_CANARY_VALIDATED`。
