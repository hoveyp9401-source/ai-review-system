# ADR-0007：Live Mutation 的并发与 Tenant 硬门禁

## 状态

Accepted，2026-07-10。

## 背景

当前 Conversation State key 没有 tenant，StateStore 只有 CAS，没有 conversation execution lease。两个不同 message 可以从同一 state version 开始并分别产生副作用。

## 决定

Phase 1 Harness 拒绝 `live` mode。正式接管线上 mutation 前必须证明：

- tenant 出现在 state/run/command/idempotency/audit key；
- state-changing Turn 有 conversation lease/fencing；
- 同库业务写、receipt、state reducer、audit outbox 同一 UoW；
- 副作用后的 CAS conflict 不重跑 command；
- remote receipt 有 Inbox 和 reconciliation；
- mutation kill switch 可按 tenant/domain/effect 生效。

## 放弃的方案

- 依赖 per-message idempotency 代替 conversation sequencing；
- 写成功后把 State CAS conflict 描述为“零写”；
- 把 team-less 请求放进默认共享 tenant；
- Phase 1 用临时锁宣称完成企业并发协议。

## Phase 1 影响

所有执行均为 Shadow/Replay simulation，`actual_write` 必须为 false。

## 后续方向

在独立 Phase 完成 tenant migration、fencing 和 RuntimeRunStore 后，设计受控 Live cohort。

