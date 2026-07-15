# ADR-0003：Context Assembly MVP，不建设复杂 Memory

## 状态

Accepted，2026-07-10。

## 背景

当前 `CognitiveTurn.resources` 是 Daily-specific dict，旧 ContextPack 又混合 personal memory、knowledge、recent actions 和 reply。继续横向扩展会产生双重上下文中心。

## 决定

Phase 1 提供：

```text
MvpContextAssembler.assemble(RuntimeTurnRequest) -> RuntimeContext
```

RuntimeContext 只包含同一次 load 得到的：

- Conversation State/version；
- current goal/entities；
- recent context；
- 全部 active pending；
- user identity；
- allowlisted request metadata；
- Daily typed planning snapshot compatibility view。

Daily snapshot 每 Turn 通过 provider 重新加载，不能在 Harness 生命周期中固定旧 version。

Snapshot provider 只接收不含正文的 typed query；raw text 只进入 Semantic Interpreter。Manifest 绑定 source text hash、Conversation State digest、Daily snapshot/report/version digest、active task digest 与 policy digest，但不把这些业务正文复制进 trace。

## 放弃的方案

- 通用 MemoryStore；
- personal-memory/RAG/Web 自动注入；
- request metadata 原样进入 Core；
- ContextAssembler 通过关键词选择 Provider；
- 把 DB session、LLM client 或 executor 放进 RuntimeTurnRequest。

## Phase 1 影响

Context MVP 是 typed 生命周期和 audit manifest，不是长期 Memory 系统。

## 后续方向

第二个真实 Domain 接入时再引入 consumer-specific ContextView 和 versioned context contract。
