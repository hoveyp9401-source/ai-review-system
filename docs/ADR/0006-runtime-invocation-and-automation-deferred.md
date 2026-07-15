# ADR-0006：自动化与交互式 Harness 分离

## 状态

Reserved，2026-07-10。

## 背景

`RuntimeTurnRequest` 是人类文本消息模型，不能表达 schedule、domain event、approval callback 或跨天 wait。当前 scheduler 已有直接业务写路径，但 Phase 1 禁止扩大为 workflow 平台。

## 决定

Phase 1 只实现交互式 Harness，并保留 tenant、invocation、causation、correlation 的演进空间。

未来架构为：

```text
UserTurn → Interactive Harness ┐
Typed Trigger → Automation Coordinator ├→ Typed Execution Kernel
```

非人类 trigger 不伪造成用户文本，也不调用 Cognitive Core；它仍不能绕过 typed command、Authorization、idempotency、Domain Executor 和 receipt。

## 放弃的方案

- Phase 1 建设 DAG/BPMN；
- scheduler 文本化后喂给 LLM；
- 用 Conversation pending 承担跨天流程；
- automation 直接调用 repository。

## Phase 1 影响

不实现 ProcessInstance、timer、signal、approval provider 或 compensation。

## 后续方向

选定第一条真实自动化流程后，先实现窄 ProcessInstance/Signal；第二条差异显著流程出现后再评估通用化。

