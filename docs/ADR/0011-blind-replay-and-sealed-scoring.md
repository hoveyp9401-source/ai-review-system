# ADR-0011：Blind Runtime Replay 与 Sealed Scoring 物理隔离

## 状态

Accepted，2026-07-10。

## 背景

ADR-0005 的临时 replay adapter 从 baseline expected/workflow/report delta 编译 semantic fixture。即使报告标注 `cognitive_semantic_independence=false`，同一数据仍同时生成 candidate 与评分，无法证明 Semantic/Safety closure。

## 决定

离线验收采用三个不可合并的 artifact：

1. `BlindInputPack`：只含 raw text、opaque identity、合法 state/resources/config；递归拒绝 oracle 字段。
2. `BlindActualArtifact`：Blind Runner 完成后生成，绑定 input digest、Runtime version hash 和 completion hash；不含 labels/scores。
3. `SealedLabelStore`：由独立 split process 生成，与 Blind Input 分文件保存；只有 scorer 读取。

运行顺序固定为：

```text
build split → run blind Runtime → seal Actual Artifact → score against labels
```

Blind Runner 的 module/CLI interface 不接受 label path；Runtime dependency graph 不 import evaluation/scorer。Scorer 在 Actual Artifact 未完成或 completion hash 不匹配时 fail closed。

Machine-proposed labels 必须标记 `independent_review_status=pending`。只有全部相关 labels 由独立 reviewer 签核后，score 才能标记 independent metrics available。

旧 baseline-derived replay 保留为 legacy orchestration diagnostic，固定 acceptance-ineligible，不得进入 Shadow Candidate Gate。

## 放弃的方案

- 在同一进程同时加载 raw input 和 expected；
- 用“代码约定不读取 expected”代替文件/interface 隔离；
- 把 baseline-derived 0 mismatch 当 Cognitive parity；
- Codex 自签 machine labels 为独立 Gold；
- Actual Artifact 生成过程中运行 scorer。

## 影响

Replay 的 candidate cognition 现在可以由真实 `LLMCognitiveSemanticInterpreter` 产生。旧 42→0 / 29→7 数字全部废弃为 closure evidence，必须通过 Blind Runtime 重裁。

## 后续

- semantic segments contract 已进入 CognitiveDecision/Actual Artifact；
- 73-case Blind pack、独立 reviewer packet 与机器候选 closure report 已生成；
- 对 842/5562 原始 corpus 恢复后执行同一 split/run/score 链；
- Production Shadow 只能复用 Blind Input/Actual Artifact 思路，不能加载 Sealed Label Store。
