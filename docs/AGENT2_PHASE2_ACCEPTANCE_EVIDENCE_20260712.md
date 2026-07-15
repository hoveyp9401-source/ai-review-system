# Agent2 Phase 2 验收证据台账 — 2026-07-12

当前裁决：`READY_FOR_TWO_USER_REAL_TEST`

Goal 执行状态：`BLOCKED`（不是代码回滚，也不是 `NO_GO`）。连续三轮 Goal 审计均未出现新的刘聪真实入站出差消息、真实 travel candidate/回复、真实用户 CaseProgress 修改/删除消息或经批准的脱敏 Party/Case 数据集；按验收规则禁止伪造这些证据。

本文件记录服务器当前事实，不把单元测试、事务回滚诊断、`server_acceptance_smoke`、`sandbox_fixture` 或页面静态展示计作真实用户端到端证据。目标 tenant 仅为 `sandbox-agent2-phase2-20260711`，测试身份仅为庞浩和刘聪。

## 运行时与隔离

| 项目 | 服务器证据 | 结论 |
|---|---|---|
| API / Stream / Scheduler | 三个 systemd 服务各 1 个进程；`/health` 返回 `{"status":"ok"}` | 已运行 |
| Business Phase 2 | 总开关、Party、CaseProgress、Travel 及写入开关均为 true | 已启用 |
| 通知 worker | Travel worker 与 Case Follow-up 独立开关均启用；follow-up tenant/user allowlist 仅含目标 tenant 和庞浩、刘聪；钉钉 app key/secret 已配置（未记录密钥值） | 已运行，已有案件追问真实发送证据 |
| Canary | route=`agent2_canary`，version=3，rollback=false | 已启用 |
| 用户范围 | 仅庞浩 `222b...9377b0`、刘聪 `91a1...803f7`；两人各只可见 3 个 Sandbox 案件 | 符合隔离要求 |
| Legal Ops | `/legal-ops/api/shell` 返回 `mode=sandbox_live`；tenant 正确；Live 导航存在 | 已部署 |
| Demo/Live 隔离 | Live 环境 `/legal-ops/api/overview` 返回 404 | 已隔离 |
| Conversation State | tenant:user 命名空间下有 1 条真实私聊状态，version=3，payload version 与行版本一致，用户属于两人 allowlist；刘聪为 0 | 庞浩已进入真实 Phase 2，租户命名空间成立 |

Round 24 部署后的安全聚焦回归（Business domain/compiler/policy/repository/schema/SQL executor、Party import、Case Follow-up、Cognitive Runtime Phase 2、Travel pipeline/notification、Legal Ops Live）：`143 passed, 1 warning`。自动测试只证明代码回归，不替代真实用户 E2E。

Round 23 部署前先上传到隔离目录并执行 `py_compile`，再备份原文件和 `.env`；备份目录为 `/home/ai_review_tunnel/codex_backups/agent2_phase2_pre_round23_20260712T204150`。部署后发现旧的手工 nohup 重启脚本与 systemd 同时拉起进程，曾短暂出现双 stream/scheduler；已停止手工进程并收敛为 systemd 各 1 实例。重启脚本随后改为检测 systemd，通过终止受监管 MainPID 让 `Restart=always` 接管，禁止再创建平行 nohup 进程。

Round 24 又用真实 PostgreSQL 外层事务执行 update → duplicate replay → query → soft delete → query。首次复现删除回执序列化时 ORM 属性过期导致 `MissingGreenlet/business_persistence_error`；外层事务完整回滚，线上业务行、receipt、audit 均无残留。修复为 flush 后在异步上下文显式 refresh，再序列化 receipt。复测结果：update executed、重复 update duplicate/actual_write=false、更新后 version=2、delete executed、删除后 version=3、默认查询排除软删除项；回滚后原记录仍为 version=1、未删除，Round24 receipt/audit 残留均为 0。Round 24 备份目录为 `/home/ai_review_tunnel/codex_backups/agent2_phase2_pre_round24_20260712T205847`。

systemd 重启脚本也完成真实演练：等待 MainPID 确实变化并轮询 `/health`，最终 API、Stream、Scheduler 各 1 个进程，健康检查 200。此前两次脚本早退问题均在演练中暴露并修复，不能把失败的中间运行算作通过。

Conversation State 另以目标 tenant:user 命名空间创建独立技术测试 conversation，使用真实 PostgreSQL CAS 完成 fencing 演练：并发创建的第二个 version=1 被拒绝；已提交 version=2 后，使用 expected_version=1 的陈旧更新被拒绝；row version 与 payload version 一致。演练行随后清理，残留为 0，未改动庞浩真实 conversation state。

## 当前 PostgreSQL 事实

以下记录数均按目标 tenant 过滤；conversation state 表无 tenant_id，按目标 tenant 命名空间查询为 0。

| 表 | 记录数 | 数据来源判断 |
|---|---:|---|
| `agent2_party_entities` | 4 | `sandbox_fixture` |
| `agent2_party_aliases` | 3 | `sandbox_fixture` |
| `agent2_party_identifiers` | 2 | `sandbox_fixture` |
| `agent2_party_case_roles` | 5 | 由 fixture 导入生成 |
| `agent2_party_relations` | 1 | 由 fixture 导入生成 |
| `agent2_party_merge_candidates` | 1 | 由 fixture 导入生成 |
| `agent2_party_conflicts` | 1 | 由 fixture 导入生成 |
| `agent2_party_source_references` | 5 | `sandbox_fixture` |
| `agent2_cases` | 3 | `sandbox_fixture` |
| `agent2_case_progress` | 2 | 1 条 `server_acceptance_smoke`，1 条真实 `robot_followup` |
| `agent2_travel_intents` | 3 | 2 条 `server_acceptance_smoke`，1 条庞浩 `real_user_message` |
| `agent2_travel_collaboration_candidates` | 0 | 无记录 |
| `agent2_notification_outbox` | 1 | 真实钉钉 `case_progress_followup`，status=sent |
| `agent2_business_command_receipts` | 10 | 含真实主体查询、出差、追问发送和追问回复 |
| `agent2_business_audit_events` | 8 | 与上述真实 Business receipt 对齐 |

## 主体知识库核验

测试主体：南京华东建设有限公司，party id `f88c2935-7711-5eb9-8e8a-23fdb0606e6f`。

| 查询项 | PostgreSQL / Live API 结果 | 来源 |
|---|---|---|
| 规范名称 | 南京华东建设有限公司 | `sandbox_fixture` |
| 别名 | 华东建设、南京华东建设股份有限公司、南京华建 | `sandbox_fixture` |
| 标识符 | 测试 USCC `91320100TEST001`；测试登记号 `REG-TEST-001` | `sandbox_fixture` |
| 原告案件 | Agent2 Sandbox 测试案件 2 | `sandbox_fixture` |
| 被告案件 | Agent2 Sandbox 测试案件 1 | `sandbox_fixture` |
| 主体关系 | 与王喜存在 `legal_representative` 关系，绑定测试案件 1 | fixture 导入生成 |
| 数据来源 | 2 条 PartySourceReference，均为 `sandbox_fixture` | `sandbox_fixture` |
| 合并候选 | 该主体无合并候选；另两条同名测试公司存在 1 个候选 | fixture 导入生成 |
| 冲突 | canonical_name 存在 1 个 open 冲突 | fixture 导入生成 |
| Legal Ops 入口 | 搜索 API、主体详情 API、主体页面、案件森林跳转均可用 | PostgreSQL Live Read Model |

Live API 证据：主体搜索返回 200 且唯一命中；主体详情返回 200，包含 aliases、identifiers、case_roles、relations、conflicts、sources。该事实证明后端表和产品入口已接通，但不证明数据是真实业务数据，也不证明用户已通过 Agent2 自然语言查询。

只读查询审计诊断：在服务器真实 PostgreSQL 事务内，以 `exact_canonical_name` 执行 `query_party_cases`，得到 1 条 executed receipt 和 1 条对应 audit，`actual_write=false`；回滚后两表对应记录均为 0。此项仅证明执行器行为，不计作真实用户 E2E。

庞浩已通过真实钉钉私聊发送“查一下南京华东建设有限公司相关案件”。事实链路：

- source message：`msgo1nN3+VFz/64MfDd7/A4KQ==`
- source channel：`dingtalk_stream`
- typed command：`query_party_cases`
- receipt：`118ac1e6-6483-5518-bec6-06b3d6d9f7dd`，executed，`actual_write=false`
- audit：`c2a89b0a-9b51-550c-9ef6-f0ee258f236d`
- 返回：2 个 fixture 案件，分别为被告案件 1、原告案件 2
- WebhookEvent：processed，钉钉回复载明主体、匹配依据和两件案件
- Legal Ops Live：200，可查看同一主体及关联案件

对话入口、查询执行、receipt/audit 和钉钉回复已真实通过；但被查询的 Party/Case 数据仍全部是 `sandbox_fixture`，因此主体知识库裁决仍为 `SANDBOX_FIXTURE_ONLY`，不能升级为真实业务知识库。

### 现有案件底表边界

本机发现并以只读方式检查了两份用户提供的真实案件底表：

- `原告案件底表 (49).xlsx`：约 2630 条案件、1786 个唯一主体名称。
- `被告案件底表 - 2026-07-10T101309.921.xlsx`：约 3531 条案件、2361 个唯一主体名称。

两表包含案件负责人、承办人、当事人、法官/电话、账户等敏感字段，且部门字段混有历史部门和空值；表中未发现可稳定覆盖主体去重所需的统一标识符/别名字段。目标约束又禁止把正式业务数据写入本轮 Sandbox tenant，因此未导入任何行。若要让 Party 数据从 `SANDBOX_FIXTURE_ONLY` 升级，必须先由用户确认一份脱敏、明确 allowlist、仅用于目标 Sandbox 的数据集；不得直接复制整张正式底表。

### 案件简称安全核验

以两张表中 6117 个有效案件作为候选全集，等距抽取 500 个案件，生成 2512 条完整案名、案号、去尾缀案名、项目简称、企业简称和 `+` 分段简称。旧解析策略出现 43 条错误自动命中；Round 23 改为：

- 内部 `case_id`、`external_case_id`、唯一案号可精确命中；
- 完整案名与其他案件名称存在包含冲突时也要求澄清；
- 只有 `source_json.confirmed_aliases` 中的已确认案件别名可以自动命中；
- 未确认的模糊简称即使只有一个候选，也返回 `needs_clarification`，禁止写入；
- 重复案号、别名冲突或多案件包含关系均列出候选，不选择第一条或最近一条。

同一 2512 条测试复测后，所有类别 `resolved_wrong=0`。项目简称中 284 条要求澄清、181 条未找到，说明安全性已闭合，但自动识别覆盖率仍需依赖后续经过确认的案件/项目别名数据，不能把“零误写”表述成“所有简称都能自动识别”。服务器真实 PostgreSQL 只读复测：庞浩、刘聪各可见 3 个 Sandbox 案件；3 个 external id、3 个案号、3 个完整案名均准确命中；“案件1”返回 1 个待确认候选，“Agent2 Sandbox 测试案件”返回 3 个候选，均未自动写入。

## 案件进展核验

当前共有 2 条进展。其中一条为服务器 smoke：

- progress id：`ac462a39-bae7-5ecb-af70-c9a5997d7fe6`
- case id：`9b5c4d4c-4569-5174-892d-4ac7fc9236e8`
- 案件：Agent2 Sandbox 测试案件 1
- summary：法院预计本周五反馈查控结果
- reporter：庞浩测试身份
- `content_origin=human_record`
- `source_channel=server_acceptance_smoke`
- version=1，未软删除

庞浩随后真实发送“今天联系了法院处理沟通agent2 sandbox测试案件 法院表示下周重新查控”。该消息缺少唯一案件序号，并且紧接主体查询上下文，Cognitive Core 将其规划为 `update_case_progress_candidate`；执行器返回 `case_target_needs_clarification`，列出 3 个可见 Sandbox 案件。对应 WebhookEvent 为 processed，但未产生 Business receipt、audit 或 CaseProgress 写入。该阻断符合“不自动选择模糊案件”的安全策略，不能计作案件进展新增通过。

原 smoke 记录有 executed create receipt 和 audit，并由 Legal Ops Live 案件森林展示，但不是庞浩本次真实钉钉消息产生。数据库中还保留一条部署前的 failed update receipt，错误为 `business_persistence_error`；该旧记录不用于证明当前版本能力。

在用户明确要求系统自行继续后，服务器手工触发了案件 1 的机器人追问任务，没有人工插入 CaseProgress：

- notification id：`fdaa0d4c-444a-5f9e-8ab5-c669db8fcaa2`
- message type：`case_progress_followup`
- outbox：pending → sent
- external message id：`LlYNyMjfAgLNBXqkoz5s2Jl9MGWEf2M2iFxR3xDJlOY=`
- dispatch receipt：`6f26713a-963d-57a4-92c9-53bdf80b731c`，`dispatch_case_progress_followup`，executed
- dispatch audit：`7b3834c2-99d0-5f94-a5c9-cbfd9beacd60`
- 无 invalid、filtered 或 flow-controlled recipient

庞浩收到后真实回复“预计下周开庭”。Agent2 从唯一、已发送、未过期且属于该用户的 follow-up 上下文绑定案件 1，再由 SQL executor 二次校验通知、收件人、案件和过期时间：

- reply source message：`msgvEgIMGmJmP8Pmqy1VpOwNg==`
- CaseProgress：`5b1b2088-8e0d-5cb3-85f7-b414a59ee736`
- summary：预计下周开庭
- `content_origin=robot_followup`
- create receipt：`8fa17f96-60f0-5d14-8cc6-84ca1af49463`，executed，actual_write=true
- audit：`3b5f5bf3-0b12-5b35-92a2-b630df44bf15`
- WebhookEvent：processed，钉钉已返回案件进展记录回执
- follow-up response_json：completed，并绑定 reply source message 与 progress id
- Legal Ops 案件森林：已出现该 progress 节点，来源显示 `robot_followup`

使用同一真实回复和同一 typed command 重放，返回原 receipt、status=duplicate、actual_write=false；CaseProgress/receipt/audit 始终为 1/1/1。

案件进展新增与机器人追问闭环裁决：`REAL_E2E_PASSED`。修改、查询、幂等和软删除已经在服务器真实 PostgreSQL 事务内连续通过，并证明回滚零残留，但仍不是用户真实消息，因此只能记为 `DATABASE_ONLY`。

## 出差协同核验

原有两条 TravelIntent 分别属于庞浩、刘聪，目的地南京市，时间为 2026-07-13 02:00–10:00 UTC，但二者均为：

- `source_channel=server_acceptance_smoke`
- source message 分别为 `phase2-travel-message-1/2`

庞浩已真实发送“明天去南京出差”，新增：

- TravelIntent：`f35cf657-072b-5139-80ce-4870449d1300`
- source message：`msgTBJk3ly3nEMJpO5rvYs19A==`
- source channel：`dingtalk_stream`
- 时间：2026-07-13 全天（Asia/Shanghai）
- receipt：`a162395d-d50a-5421-b70e-1b391be5fdbe`，executed，`actual_write=true`
- audit：`16c4106f-e01b-54cb-bdaa-a4c50ed1ef25`
- WebhookEvent：processed，钉钉已回复“已完成：出差计划登记”
- Legal Ops Live：200，data_origin=`real_user_message`

真实事件重放首次发现 datetime fingerprint 未统一时区：原命令为 `+08:00`，PostgreSQL 读回为等价 UTC，旧算法会生成第二套幂等键。诊断事务内曾出现 2/2/2，但完整回滚，线上未残留重复数据。修复后 datetime 在 fingerprint 中统一转为 UTC，并将该真实记录的技术幂等键从旧算法迁移为规范键，业务字段未改变。使用同一 source_message_id、同一 typed command 再次重放，返回原 receipt `a162395d-d50a-5421-b70e-1b391be5fdbe`，status=`duplicate`、`actual_write=false`；TravelIntent/receipt/audit 在事务内外始终为 1/1/1。

庞浩单人出差登记真实闭环已通过。刘聪没有发送真实消息；其旧 smoke TravelIntent 被来源围栏排除，不能参与真实匹配。因此当前结果仍为：

- candidate：0
- notification outbox：0
- external message id：无
- transport receipt：无
- 用户回复状态：无
- 重复发送：无发送可验证

出差登记已由庞浩真实通过；两人协同目前仍未完成真实消息闭环。不得把刘聪旧 smoke 记录或人工模拟当成真实第二人事件。

出差协同裁决：`DATABASE_ONLY`。

## 页面数据来源隔离

| 页面能力 | 当前来源 | 标记 |
|---|---|---|
| 日报 | Live Phase 2 当前无日报记录 | `real_postgresql`（0 条） |
| 出差 | 两条 smoke TravelIntent | `server_acceptance_smoke` |
| 协同候选 | 无记录 | `real_postgresql`（0 条） |
| 通知 | 1 条真实案件追问通知，status=sent | `robot_followup` / `real_postgresql` |
| 案件进展 | 一条 smoke、一条真实追问回复进展 | `server_acceptance_smoke` + `robot_followup` |
| 原告/被告 | fixture 导入的 PartyCaseRole | `sandbox_fixture` |
| 主体别名/标识符 | fixture 导入 | `sandbox_fixture` |
| 案件森林枝条 | PostgreSQL Party → Role → Case → Progress 动态生成 | 混合 `sandbox_fixture`、`server_acceptance_smoke` 与 `robot_followup`，逐条标识 |
| receipt / audit | fixture import、server smoke 与真实钉钉链路 | `system_generated`，source message 与 data origin 可追溯 |
| Demo Seed | Live 模式不读取；Demo API 为 404 | 已隔离 |
| frontend static 业务数据 | Live Read Model 未使用 | 无 |
| mock transport | Live 数据中不使用；案件追问具有真实 external message id | 无 mock 冒充 |

## 能力矩阵

| 能力 | 真实对话 | PostgreSQL | Receipt/Audit | Legal Ops | 真实通知 | 用户回复 | 当前结论 |
|---|---|---|---|---|---|---|---|
| 主体知识库查询 | 庞浩真实通过 | fixture 数据可查 | 真实 receipt/audit | 可搜索和查看详情 | 不适用 | 钉钉已返回查询结果 | `SANDBOX_FIXTURE_ONLY` |
| 主体详情 | 不适用 | fixture 数据可查 | import receipt/audit | 已展示 | 不适用 | 不适用 | `SANDBOX_FIXTURE_ONLY` |
| 案件进展新增 | 机器人追问回复真实通过 | 真实 robot_followup 记录 | 真实 receipt/audit | 案件森林已展示 | 真实追问已发送 | 庞浩真实回复 | `REAL_E2E_PASSED` |
| 案件进展修改 | 无真实用户消息 | 真实 PostgreSQL 回滚事务连续通过 | 事务内 receipt/audit 正确，回滚零残留 | 未形成线上真实更新 | 不适用 | 不适用 | `DATABASE_ONLY` |
| 案件进展删除 | 无真实用户消息 | 软删除、version、默认查询排除均在真实库回滚事务通过 | 事务内 receipt/audit 正确，回滚零残留 | 未形成线上真实更新 | 不适用 | 不适用 | `DATABASE_ONLY` |
| 案件进展查询 | 无独立真实用户查询 | 真实 PostgreSQL 回滚事务通过 | query receipt/audit 正确 | Live 已展示当前有效进展 | 不适用 | 不适用 | `DATABASE_ONLY` |
| 案件森林实时更新 | 追问回复真实发生 | robot_followup 枝条存在 | 真实 receipt/audit | 动态读取 PostgreSQL 并已显示 | 真实追问 | 庞浩真实回复 | `REAL_E2E_PASSED` |
| 出差登记 | 庞浩真实通过 | 1 条真实、2 条 smoke | 真实 receipt/audit | 已展示真实来源 | 不适用 | 钉钉已返回登记结果 | `REAL_E2E_PASSED` |
| 同期同地匹配 | 未发生 | 真实 source 无记录 | 无 | 无 candidate | 不适用 | 不适用 | `CODE_ONLY` |
| 出差协同钉钉通知 | 未发生 | travel outbox=0 | travel transport receipt=0 | 无 travel 通知 | 无 travel external id | 无 | `CODE_ONLY` |
| 接受/拒绝 | 未发生 | 无 candidate response | 无 | 无状态 | 无 | 无 | `CODE_ONLY` |
| 机器人案件追问 | 庞浩真实通过 | outbox 与 CaseProgress 已落库 | 发送和回复均有 receipt/audit | 全部显示为 robot_followup | external id 存在 | 已完成并绑定 progress | `REAL_E2E_PASSED` |

## `TWO_USER_REAL_SANDBOX_READY` 19 项完成审计

| # | 门槛 | 当前证据 | 审计结论 |
|---:|---|---|---|
| 1 | Business Phase 2 真实入口部署 | API/Stream 运行代码与真实庞浩消息 receipt | 已证明 |
| 2 | 仅庞浩、刘聪进入 Canary | route version=3；canary/identity/配置 allowlist 精确为两人 | 已证明 |
| 3 | 主体知识库自然语言真实通过 | 庞浩真实查询已通过，但 Party/Case 为 fixture | 链路已证明，真实业务数据未证明 |
| 4 | Legal Ops 主体详情读取 PostgreSQL | Live API 200；主体详情、角色、来源可查 | 已证明 |
| 5 | CaseProgress 新增/查询/修改/软删除真实通过 | 新增为真实追问回复；其余仅真实库回滚诊断 | 未证明完整真实用户 CRUD |
| 6 | CaseProgress 案件森林实时更新 | 真实 robot_followup 进展已显示 | 已证明 |
| 7 | 日报或机器人追问形成真实枝条 | 机器人追问、真实回复、枝条均存在 | 已证明 |
| 8 | 两人真实消息生成 TravelIntent | 仅庞浩真实；刘聪只有旧 smoke | 未证明 |
| 9 | 唯一协同候选 | candidate=0 | 未证明 |
| 10 | 双方真实收到钉钉通知 | 只有庞浩案件追问，不是 travel 双方通知 | 未证明 |
| 11 | transport receipt 完整 | 案件追问完整；travel 不存在 | travel 未证明 |
| 12 | 双方回复更新协同状态 | 无 travel candidate/reply | 未证明 |
| 13 | 不重复写入/通知 | 真实庞浩 TravelIntent 与追问回复幂等通过；travel candidate 通知未发生 | 部分证明 |
| 14 | State 隔离与 fencing | 真实 state 使用 tenant:user key；真实 PostgreSQL CAS 拒绝重复创建与陈旧 expected_version，技术演练行清理为 0 | 已证明 |
| 15 | 三级 kill switch | tenant/domain/effect 开关与精确 allowlist 已部署 | 已证明配置，未逐个真实关停通知 |
| 16 | 显式回滚演练 | route 1→2→3，计数不变，无自动 fallback | 已证明 |
| 17 | 服务器 `/legal-ops/` 可访问 | Live API 200 | 已证明 |
| 18 | 页面区分实时/smoke/fixture | Read Model 逐条 data origin；Demo API 404 | 已证明 |
| 19 | 无其他用户/正式租户受影响 | 配置、route、identity 精确 allowlist；业务表只操作目标 tenant | 已证明范围控制；无扩圈 |

审计结果不满足 19 项全绿，因此最终裁决不能是 `TWO_USER_REAL_SANDBOX_READY`。当前代码和运行环境可继续真实两用户验收，裁决保持 `READY_FOR_TWO_USER_REAL_TEST`。

## 仍需真实用户完成的验收

1. 案件进展修改和软删除仍需真实用户消息；当前仅有事务回滚的 operator/synthetic 诊断。
2. 刘聪仍需真实发送南京出差信息，生成唯一 candidate 和双方 travel outbox。
3. Travel worker 真实发送双方协同通知，保存 external message id、transport response、receipt 和 audit。
4. 双方真实回复，状态回写并由 Legal Ops 刷新展示。
5. 用户确认一份脱敏、显式 allowlist 的 Party/Case Sandbox 数据集；当前真实案件底表不能直接写入本轮 tenant。

最后一次外部状态复核：目标 tenant 仍为 TravelIntent=3、travel candidate=0、notification=1（仅已发送案件追问）、CaseProgress=2、Business receipt=10、Business audit=8；Round24 技术验证残留 receipt/audit 均为 0。最新刘聪 webhook 仍停留在 2026-07-11 00:33 UTC，最新庞浩 webhook 为 2026-07-12 11:53 UTC，之后没有新的两人入站事件。API、Stream、Scheduler 各 1 个进程，Legal Ops Live 200，故阻断来自缺少验收所需的真实外部事件/授权数据，不是服务器不可用。

在上述证据产生前，不得裁决 `TWO_USER_REAL_SANDBOX_READY`，也不得声称 Agent2 已替代 Agent1。
