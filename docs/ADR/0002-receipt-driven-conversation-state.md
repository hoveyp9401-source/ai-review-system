# ADR-0002：Harness 吸收 Orchestrator，并以 receipt 决定 State 推进

## 状态

Accepted，2026-07-10。

## 背景

`CognitiveOrchestratorV3` 当前在 typed executor 前保存 Core proposed state。若 command 随后被 version conflict、权限或目标校验阻断，pending 可能先被消费。

## 决定

Harness 直接调用：

```text
Core.process → Planner.plan → DomainPack.execute → receipts → StateStore.save
```

不再调用或包装 `CognitiveOrchestratorV3`。

Phase 1 最小规则：

- simulated/succeeded receipt：允许推进隔离的 replay state；
- clarification 且无失败 command：允许推进隔离的 replay state；
- unavailable/blocked/failed receipt、未知 action 或 Planner block：保留原 state；
- state save 必须发生在 domain receipt 之后。

Shadow 不推进生产 Conversation State；其 proposed state 只进入 trace/evaluation。Phase 1 使用整轮保守规则，并不冒充 operation-level reducer。

## 放弃的方案

- 无条件保存 Core next state；
- blocked command 后只补偿 pending；
- executor 自行修改 Conversation State；
- 把完整 StateTransition 平台提前放入 Phase 1。

## Phase 1 影响

Replay 多轮对话具有安全的本地连续性。此规则不宣称已解决生产并发。

## 后续方向

Live 前引入 operation-level reducer、conversation fencing 和同数据库 Unit of Work；远程 mutation 使用 receipt consumer。

## Phase 2 production wiring (2026-07-11)

The Stream and Webhook primary paths now use a two-stage state transition. The
orchestrator returns both the loaded base state and the proposed next state.
Turns with typed commands or planner blocks do not save the proposal. After
execution, the runtime requires one successful/duplicate result for every
planned Daily and Business action and rejects partial result sets. Only then is
the proposed state saved once with optimistic version checking, with any bound
pending consumed in that same save. Failed, blocked, missing or partial outcomes
leave the loaded base state unchanged. Clarification/chat turns with no command
and no planner block may still persist immediately.
