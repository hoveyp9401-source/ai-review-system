# ADR-0001：Phase 1 采用 Shadow/Replay-first Runtime Harness

## 状态

Accepted，2026-07-10。

## 背景

当前 Cognitive Core v3、Planner 和 Typed Daily Executor 已存在，但 Stream、Webhook、Manual 仍分别编排 cognition、execution、commit、reply 和 audit。完整 Live Runtime 还缺 tenant-aware state、conversation fencing、durable run ledger 和 receipt reducer。

## 决定

Phase 1 新增唯一交互 interface：

```text
Agent2RuntimeHarness.handle(RuntimeTurnRequest) -> RuntimeSuccessOutcome | RuntimeFailureOutcome
```

Harness 完整运行 Context → Core → Planner → DomainPack → typed executor simulator → state → reply → audit。Composition root 只能选择 `shadow` 或 `replay`；request metadata 和用户正文不能选择模式。

Turn 生命周期中的 Context/Core/contract/Planner/Executor/State/Audit 错误返回 `failed_closed` outcome，携带稳定 `failed_stage/error_code`，不调用 legacy。构造期的非法 composition 仍直接拒绝启动。

显式 audit sink 成功返回后才产生 `audit_recorded`；未配置 sink 只能产生 `audit_skipped`，sink 异常形成 `failed_closed/audit_failed`，不得伪称已记录。

Phase 1 outcome 明确区分：

- `actual_write=false`：没有真实业务写入；
- `would_write=true`：typed simulator 判定 Live adapter 将执行写入。

Shadow 只读生产 Conversation State，绝不调用其 `save()`；Replay 只允许隔离的 ephemeral StateStore 维持多轮连续性。

## 放弃的方案

- 直接把 Harness 接到线上日报 mutation；
- 给现有三入口再包一个只转发的 facade；
- 发生异常时回退 legacy write；
- 用用户 request 字段选择 Live/Replay。

## Phase 1 影响

允许实现和验证完整 Runtime 生命周期，但不改变线上业务 Source of Truth。旧生产链路仍是独立 cohort，不是 Harness fallback。

## 后续方向

完成 ADR-0007 的 tenant、sequencing、fencing 和事务门禁后，另行批准 Live adapter。
