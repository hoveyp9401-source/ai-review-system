# ADR-0009：Runtime Ledger、Domain Event 与 Artifact Governance

## 状态

Reserved，2026-07-10。

## 背景

RunStore、Trace、Audit、Episodic Journal 存在职责重叠；Runtime trace 与业务 Domain Event 也不能共用一个 schema。法务数据需要 retention、encryption 和 legal-hold 语义。

## 决定

Phase 1 只产生内存 trace 和 Replay artifact，不建设通用 Ledger 平台。

未来固定以下区分：

- Runtime Event：Runtime 做了什么；
- Domain Integration Event：权威业务事务提交了什么；
- Projection Receipt：某 view 处理到哪里；
- View Directive：用户要求事实出现在哪个 view。

生产 ArtifactRef 必须携带 tenant、classification、retention、key ID、residency、legal hold、hash 和 redaction policy。

## 放弃的方案

- 把所有 Domain 改成 Event Sourcing；
- Trace、Audit、Memory 三套权威日志；
- 把敏感正文永久写入不可变 event row；
- Phase 1 建设完整 artifact 管理后台。

## Phase 1 影响

Replay 输出包含 input SHA-256 manifest、trace 和 mismatch；不宣称具备生产 Ledger 恢复能力。

## 后续方向

RuntimeRunStore 与 append-only Ledger 同事务；Trace/Audit/Replay export 由其 projection 产生。

