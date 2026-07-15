# ADR-0005：旧链路只作隔离 Replay Baseline

## 状态

Accepted，2026-07-10。

## 背景

旧 Harness 和 Daily replay 均依赖 WorkflowRouter、gate、legacy DailyCommand 和 fallback，不能成为 Runtime Harness runner。历史语料仍有回归价值。

## 决定

Replay 采用双通道：

```text
DialogueCase
  ├─ isolated legacy baseline adapter
  └─ candidate semantic source → Agent2RuntimeHarness replay adapters
```

旧链路在 Harness 外生成 baseline observation；Harness 不 import 或调用 legacy module。

当前本地 corpus 没有独立 recorded-v3 semantic tape。Phase 1 adapter 暂从 baseline report delta 编译 semantic fixture，因此该结果只能称为 `baseline_derived_planner_executor_replay`，并固定标记 `cognitive_semantic_independence=false`；它不能证明 Cognitive Core parity。真正的 Cognitive replay 必须让 candidate 使用独立 semantic tape 或实际 interpreter，baseline 只参与 diff。

Replay 独立计算：

- candidate replay actual-write violation；
- Gold/expected 为 no-write 时的 unexpected `would_write`；
- expected write 未发生；
- legacy fallback；
- typed-executor bypass；
- baseline/candidate classified diffs；
- input file SHA-256 manifest。

Safety 与 parity 分开：`safety_ready` 不代表 `parity_ready`，任何报告必须同时展示 mismatch 数与 evaluation scope。

重复 dialogue/turn ID fail-fast，不采用 first-wins。

## 放弃的方案

- 在 Harness 失败时调用旧链路；
- 复用旧 `gray_ready` 作为新 Safety Gate；
- 无 before-state 时从空状态推断 production parity；
- 自然语言逐字比较。

## Phase 1 影响

本地可复跑语料与服务器 842/5562 声明分开报告。缺失 corpus 不以 summary 文件代替。

## 后续方向

导出脱敏 Replay Bundle，包含 state/resource before snapshot、recorded semantic/tool result 和 runtime digest。
