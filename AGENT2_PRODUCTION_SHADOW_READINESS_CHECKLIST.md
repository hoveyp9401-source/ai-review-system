# Agent2 Production Shadow Readiness Checklist

日期：2026-07-10

本清单只裁决能否进入下一阶段评审，不授权 Live，也不授权实际启用 Shadow。

| Gate | 当前证据 | 状态 |
| --- | --- | --- |
| 只读输入副本 | `ShadowRuntimeInput` 无 Session/Repository/executor capability | offline-pass |
| Ephemeral State | 每次调用新建 `InMemoryConversationStateStore` | offline-pass |
| 禁止生产 executor | adapter 仅接受精确 `OfflineShadowEvaluator`，内部仅 in-memory simulator | offline-pass |
| 禁止生产写 | Harness + adapter 双层拒绝 `actual_write=true` | offline-pass |
| 不向用户返回结果 | `ShadowObservation` 无 reply/raw text 字段 | offline-pass |
| 独立日志 | `ShadowLogSink` contract 与最小事件 schema 已定义 | contract-only |
| Kill switch | 默认开启 | offline-pass |
| Sampling | 基于 trace id 的 deterministic sampling | offline-pass |
| Timeout | `asyncio.wait_for`，失败不传播到 Live | offline-pass |
| Circuit breaker | threshold/cooldown/open skip | offline-pass |
| PII 最小化 | identity 加盐 hash；正文仅 SHA-256/长度 | offline-pass |
| Trace correlation | 保留 trace id | offline-pass |
| Live/Shadow 物理隔离 | 独立 composition；无入口 wiring | offline-pass / deployment-pending |
| 独立人工 semantic review | 1407 条 reviewer packet，human approved=0 | blocked-external |
| 精确 842/5562 parity corpus | 本地未恢复 | blocked-external |
| 隔离 PostgreSQL smoke | Docker 缺失、localhost:5432 不可达；transaction simulator 7/7 | blocked-environment |
| 生产只读复制与日志基础设施评审 | 尚未提供/连接 | blocked-external |

## 启用前最小外部动作

1. 独立 reviewer 审核高风险 semantic packet 并生成签名 adjudication；
2. 导出精确 842/5562 raw corpus 与 selection/hash manifest；
3. 提供隔离 PostgreSQL 或容器环境，运行真实 rollback/lock/idempotency smoke；
4. 由安全/平台负责人评审只读复制、独立日志、网络与密钥边界；
5. 以上通过后另行审批生产 Shadow wiring。仍不得直接进入 Live。
