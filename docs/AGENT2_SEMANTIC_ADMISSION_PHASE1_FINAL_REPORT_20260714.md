# Agent2 跨领域语义准入与组合 Phase 1 最终报告

日期：2026-07-14  
最终裁决：`SHADOW_ONLY`

## 1. 结论

本轮已完成统一 Semantic Proposal → Domain Admission → typed Planner/Executor → receipt/Outcome → receipt-driven state/reply 的 Phase 1 生产底座，并部署到当前服务器的限定 Shadow 范围。

当前只允许：

- 1 个既有 Agent2 Sandbox tenant；
- 庞浩、刘聪两个既有受保护用户身份；
- audit-only Shadow Decision/Review 采集；
- 现有 legacy 业务行为继续运行。

当前明确禁止：

- Semantic Admission Enforce；
- Deferred 自动续办；
- Case Follow-up 主动发送；
- Case → Report 自动投影；
- 扩大用户或 tenant；
- 宣称 Agent2 已替代 Agent1。

本轮不裁决 `ZERO_SEMANTIC_ERROR`。目标是让候选误判无法越过确定性合同，而不是声称模型永不误判。

## 2. 架构结果

生产主链已收敛为：

```text
Webhook / Stream / Manual
→ VerifiedTurnRequest
→ one LLM Semantic Proposal
→ deterministic DomainAdmissionEngine
→ per-segment Decision / Ticket / Pending
→ typed Planner
→ Report / Case / Travel Executor revalidation
→ committed receipt
→ OperationOutcome
→ receipt-driven Conversation State
→ OutcomeReplyComposer
```

核心边界：

- LLM 只提出候选事实、动作和 evidence span；
- mutation 必须持一张 exact-scope Ticket；
- Executor 在同一事务内重验对象、权限、版本、状态和 Ticket；
- Ticket 不证明写入，只有 committed receipt 能形成成功 Outcome；
- Reply 只解释 Outcome，不重新判断成功、送达或接受；
- 一个歧义 segment 只阻断自身，合法 sibling 保留；
- Information/Selection continuation 必须 fresh Admission；
- Review/Deferred 只保存与 Decision 精确绑定的 digest-only 三键，不保存自由正文，也不能成为写权限。

详细合同与 Policy Matrix 见 `docs/AGENT2_SEMANTIC_ADMISSION_PHASE1.md`、`docs/ADR/0019-domain-admission-and-composition.md` 和 `docs/schemas/agent2-domain-admission.schema.json`。

## 3. 三领域安全合同

### Report

- 显式报告动作或唯一兼容 active Report Task 才可写；
- 报告 ID、日期、状态、版本和稳定 item ID 来自 trusted resources；
- “没其他风险”在风险追问中为 no-op，不存正文、不转 Case；
- 日报、周报、月报走统一上层 Report Command/Outcome；
- 每次变更后完整展示用户可见快照，不泄露内部字段。

### CaseProgress

- 必须唯一解析到权限内案件；
- exact segment 必须包含可举证的断言事实/动作/状态/计划/准备/阻塞；
- 通用“案件材料”、问题、否定、假设、引用、产品说明不能写入；
- 改删使用稳定 ID/version；
- Case 成功后 Report 投影只能来自 committed receipt；当前自动投影保持关闭。

### Travel

- 仅当前用户明确、实际、非否定/假设/转述的行程；
- 地点和时间必须唯一、可解析；
- 协同回复绑定唯一同 scope、未过期、未消费对象；
- provider accepted 不等于送达或同意；
- 接受、拒绝、取消、过期等协同事实不再依赖 operation 名，必须有对应 committed database response receipt。

## 4. Outcome、Pending 与幂等加固

- OutcomeStore 使用调用方 tenant/user/conversation/source_turn 作为唯一可信 scope；
- 同一幂等键只有全部持久化事实精确一致才返回 duplicate；
- `OutcomeObjectRef.object_label/object_version` 已进入 ORM、migration、写入和冲突比较；
- label/version 漂移现在 fail-closed；
- Selection terminal audit 与 ConversationState CAS 同一 SAVEPOINT；
- 多 Pending、过期、跨 tenant/user/conversation、权限撤销、候选删除/版本变化均零业务写；
- Webhook/Stream/Manual 的 blocked/read-only/periodic Outcome 均持久化后再回复；
- Webhook 日志只保留元数据、长度、哈希和异常类型，不输出 callback credential 或明文 payload。

## 5. 测试证据

最终冻结树：

| 测试集 | 结果 |
|---|---:|
| Agent2 | `1596 passed, 1 skipped` |
| Legal Ops | `77 passed` |
| 全仓 | `142 failed, 2179 passed, 1 skipped` |

全仓 142 个失败节点与既有基线逐条完全一致：

- 136 个 deprecated legacy Report protocol；
- 6 个隔离的 legacy `agent_core`；
- Agent2 Runtime 直接回归：0；
- 没有删除、skip 或 xfail 既有失败以制造全绿。

新增覆盖包括：

- Report + Case + Travel 同轮三张 Ticket；
- 同一个权威 Ticket ledger 中三票全部 `consumed`；
- 歧义 Case sibling 被单独阻断；
- Webhook/Stream/Manual parity；
- fresh Selection/Information Admission；
- state CAS、重复 ingress、跨 scope、权限/版本/TTL 漂移；
- Outcome/message/transport 事实一致性；
- Review/Deferred 与 Decision action/verdict/evidence 精确绑定；
- Outcome label/version 幂等冲突；
- PostgreSQL migration、回滚、并发和幂等。

回归工件位于 `artifacts/agent2-semantic-admission-phase1/regression/`。

## 6. PostgreSQL

两个当前 hash 的 migration 均在配置的真实 PostgreSQL 临时 schema 中通过首次执行、幂等重放、回滚、public 不变和清理：

- Semantic Admission：`ac96c379ae93852d54896f60dc14213e9974c7bd760fe01490a9cea0d79c1aaa`；
- Case Lifecycle / Outcome：`19b19a8653fdb4ba96bc2e300c886086b90a45982bb7d05b5cc7919718bdf34c`。

随后两个 hash-pinned migration 均成功应用到服务器 `public`。六张 Admission 表存在；部署后只读检查时均为 0 行，说明还没有当前版本的真实用户 Shadow 消息证据。

结构性违规为 0，Outcome 的 `object_label/object_version` 列已存在。

## 7. 服务器部署

- 运行文件：46 个 path-allowlisted 文件；
- Runtime bundle SHA-256：`d20e3523f73671364f9187d89e8d609733abfd0f5bf83423d6a99e7bdf167031`；
- 服务器 Python 3.11 compile/import：PASS；
- `.env`：`0600`；
- API / Stream / Scheduler：各 1 个生产实例；
- health：`ok`；
- staging API 8010：未修改；
- Follow-up send / Report projection：仍为 false。

部署先备份再覆盖。第一次正式脚本因 CRLF/LF canonical hash 口径不同，在 migration/config/restart 前安全中止；原备份未被覆盖。统一为 LF 后重试成功，并复用首次回滚点。

部署清单、hash、migration receipt、配置 receipt 和只读校验见：

- `artifacts/agent2-semantic-admission-phase1/release-manifest-final-20260714.json`；
- `artifacts/agent2-semantic-admission-phase1/server-deploy-20260714/`；
- `docs/evidence/agent2_semantic_admission_server_shadow_20260714.md`。

## 8. Blind Replay

旧的 26-case actual 属于旧 runtime hash，仅能作为修复输入，不能证明当前版本。

冻结部署后的新运行只执行了一次。模型给出缺少 `daily_item_target` 的 `edit_daily_item`，生产合同正确 fail-closed，但 Blind runner 在发布 actual 前终止：

- actual artifact：未创建；
- sealed labels：runner 不可见；
- scoring：未执行；
- retry：未执行；
- acceptance eligible：false。

这次失败被保留为 `evals/agent2/semantic_admission/actual_after_fix_failure.json`，没有通过重复运行挑选更有利的非确定性结果。

## 9. 未关闭问题

| 问题 | 当前措施 | 对裁决影响 |
|---|---|---|
| Shadow legacy Selection/projection early return 无 Admission trace | Enforce 已绕开；Shadow 数据不得用于升 Enforce | 阻断 Enforce |
| Frozen-runtime Blind 不可评分 | 保留失败，不重跑挑样 | 阻断 Enforce |
| 无本版本两用户真实消息证据 | 不冒充用户、不手工造闭环 | 阻断 Canary validated |
| Case 文档关联缺 trusted document resource | fail-closed | 能力不完整 |
| Case → Report durable receipt handoff 未完成 | 自动投影关闭 | 阻断自动投影 |
| legacy compatibility 权限撤销竞态 | 不作为 Enforce 证据 | 阻断扩大范围 |
| Follow-up Selection 权威元数据不足 | Enforce fail-closed | 能力不完整 |

完整台账见 `docs/evidence/agent2_semantic_admission_open_issues_20260714.md`。

## 10. 最终裁决

```text
SHADOW_ONLY
```

理由：

- 本地实现、Standards Review、Spec Review、Agent2/Legal Ops 回归、PostgreSQL 和服务器部署均满足 Shadow 条件；
- 服务器范围、开关、单实例、hash 和结构性安全均可审计；
- 但新 Blind 未形成可评分 actual；
- 尚无庞浩、刘聪当前 Runtime 的真实 Shadow/Enforce 闭环；
- Shadow 观测仍有 legacy early-return 缺口；
- 若干 authority 能力仍安全降级。

因此允许继续收集两用户范围内的 audit-only Shadow 证据，不允许开启 Enforce，不允许扩大 Canary，也不能裁决 Agent2 替代 Agent1。

机器可读裁决：`artifacts/agent2-semantic-admission-phase1/verdict-final-20260714.json`。
