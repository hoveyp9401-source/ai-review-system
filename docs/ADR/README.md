# Agent2 Runtime ADR Index

本目录记录 Agent2 Runtime Harness 的关键架构决定。ADR 只固定决定、约束和启用门禁，不代表相关平台已在 Phase 1 实现。

| ADR | 状态 | 主题 |
|---|---|---|
| [0001](0001-phase1-shadow-replay-runtime-harness.md) | Accepted | Phase 1 采用 Shadow/Replay-first Harness |
| [0002](0002-receipt-driven-conversation-state.md) | Accepted | Harness 吸收 Orchestrator，state 在 receipt 后推进 |
| [0003](0003-context-assembly-mvp.md) | Accepted | Context Assembly MVP，不建设复杂 Memory |
| [0004](0004-static-domain-pack-daily-only.md) | Experimental | 静态 Domain Pack，Phase 1 只执行 Daily |
| [0005](0005-replay-baseline-isolation.md) | Accepted | 旧链路只作隔离 baseline，不能成为 fallback |
| [0006](0006-runtime-invocation-and-automation-deferred.md) | Reserved | 自动化与交互式 Harness 分离 |
| [0007](0007-live-concurrency-and-tenant-gate.md) | Accepted | Live mutation 的并发与 tenant 硬门禁 |
| [0008](0008-cognitive-catalog-and-reference-resolution.md) | Reserved | Versioned Cognitive Catalog 与有界实体解析 |
| [0009](0009-runtime-ledger-events-and-artifacts.md) | Reserved | Runtime Ledger、Domain Event 与数据治理 |
| [0010](0010-pending-and-approval.md) | Accepted | Conversation pending 与企业 approval 分离 |
| [0011](0011-blind-replay-and-sealed-scoring.md) | Accepted | Blind Runtime Replay 与 Sealed Scoring 物理隔离 |
| [0012](0012-offline-production-shadow-candidate-adapter.md) | Accepted | 离线 Production Shadow Candidate 能力隔离边界 |
| [0013](0013-legal-operations-phase0-sandbox-boundary.md) | Accepted | Legal Operations Phase 0 tenant-isolated Sandbox boundary |

所有 Phase 1 工程任务必须同时遵守以下不变量：

- `CognitiveDecisionV3` 不产生数据库动作；
- 所有 Daily 执行只能消费 `TypedDailyCommand`；
- Harness 不 import 或调用 legacy router、gate、adapter、fallback；
- Shadow/Replay 中 `actual_write=false`，模拟副作用使用 `would_write=true`；
- raw text 只进入 Semantic Interpreter，Snapshot Provider 与 Domain Executor 的类型中不存在正文；
- Shadow 不保存生产 Conversation State，Replay 只能 checkpoint 到隔离 ephemeral store；
- 每个 action → command/block、每个 command → owner/receipt 必须完整闭合；
- Turn 生命周期错误必须形成可审计 `failed_closed` outcome；
- 未接入 Domain command 必须显式产生 receipt，不能静默丢弃。
