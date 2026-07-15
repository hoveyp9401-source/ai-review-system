# ADR-0008：Versioned Cognitive Catalog 与有界 Reference Resolution

## 状态

Reserved，2026-07-10。

## 背景

当前 action/entity vocabulary、Prompt 和 Planner switch 仍有中央硬编码；案件名称等自然语言 hint 也不能直接成为稳定 mutation target。

## 决定

Phase 1 对当前可执行/可规划 action 使用静态 closed parameter/entity-attribute allowlist，并建立 Daily exact contract 和 owner 校验；不建设动态 catalog。未知字段在 Planner 前 fail closed，不能仅依赖数据库关键词黑名单。

未来必须引入：

- `action_contract_ref/schema major`；
- 由 versioned catalog 生成 recursive closed entity/action body；
- immutable CapabilityCatalogSnapshot；
- catalog 生成的 Prompt contract summary；
- 最终 mutation plan 前的 typed read-only ReferenceResolution：

```text
EntityHint → BoundRef | Ambiguous | NotFound → first mutation plan
```

Execution 阶段仍禁止 plan expansion。

## 放弃的方案

- 关键词/正则选择 Domain；
- 模型输出 tool/handler 名称；
- ToolResult 直接补写已持久化 mutation command；
- Phase 1 建设动态 Catalog/Marketplace。

## Phase 1 影响

只有当前 Core 已支持的 action 可被 Runtime 使用；未知 action 显式 block。

## 后续方向

以 Case query 或 Knowledge read 作为第二个真实 capability，验证 Catalog 后再发布稳定 v1。
