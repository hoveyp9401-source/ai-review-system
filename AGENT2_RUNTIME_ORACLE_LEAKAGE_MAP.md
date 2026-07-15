# Agent2 Runtime Oracle Leakage Map

日期：2026-07-10

## 裁决

旧 `baseline_derived_planner_executor_replay` 同时用 baseline/expected 编译 candidate cognition 与评分，原 42→0 / 29→7 结果永久标记为 `oracle_assisted_diagnostic_only`，不能作为 closure evidence。

新的验收链路物理拆成三个 artifact 和两个进程：

```text
Scored source corpus
  └─ offline split process
      ├─ Blind Input Pack ──> Blind Runner process ──> completed Actual Artifact
      └─ Sealed Label Store ─────────────────────────> scorer process
```

Blind Runner 的 constructor/CLI 没有 label、expected、baseline 或 scorer 参数，`app.agent2.runtime` 不 import `app.agent2.evaluation`。Scorer 只接受 pack id/digest 相同、`completed=true` 且 completion hash 正确的 Actual。

## 泄漏源与处置

| Oracle source | 旧传播路径 | 风险 | 新处置 |
| --- | --- | --- | --- |
| `expected.*` | dialogue → `RuntimeReplayTurn.expected` → baseline semantic compiler | 直接决定 workflow/action/write | 只写 Sealed Labels；Blind parser 递归拒绝 |
| `baseline.direct_write` | legacy result → candidate cognition | 直接决定日报 action | legacy replay acceptance-ineligible；Blind Runner 不导入 |
| report before/after delta | historical actual → entity/action compiler | 把历史执行结果冒充 cognition | 只保留 diagnostic；Blind resources 只含合法当前 snapshot |
| expected command/domain/write | source labels → candidate/scorer | candidate 与 scorer 共用 oracle | split 后只在 sealed file；Runner process 无路径参数 |
| closure/root cause/score/reviewer | ledger/report output | 反向影响新 actual | 递归拒绝；只在后置 evaluation join |
| model/prompt version omission | 不同语义实现生成同 run_id | 证据不可复现 | Runtime hash 覆盖 prompt/contract/code + model/thinking identity |

## Blind 允许面

- raw text 与 prior turns；
- opaque case/turn/conversation identity；
- legal Conversation State；
- legal current snapshot resources；
- allowlisted runtime config/active task snapshot；
- timezone-aware occurrence time 与非语义 external message id。

递归禁止：`expected`、`baseline`、`gold`、`reference_decision`、`expected_write_intent`、`expected_domain`、`expected_command`、`historical_actual_output`、`closure_result`、`root_cause_annotation`、`score`、`reviewer_conclusion`。

## Actual Artifact 闭包

Actual 由 Runner 结束时一次性发布，包含：

- pack digest、input hash、Runtime version hash、run id、completion hash；
- goal、entities、exact semantic segments；
- domain ownership、action class、write intent、clarification、executable status；
- typed commands、PlanningBlocks、domain receipts、outcome/error/stage/trace/state；
- `actual_write`、`would_write`、`legacy_fallback_used`。

Actual 不包含 labels、expected、baseline、score 或 reviewer conclusion。文件通过临时文件原子替换发布，读取时重新验证 completion hash。

## 自动证明

- expected label 替换、write intent 反转和 poison baseline 不能改变 Blind pack/Actual；
- interpreter 在模型调用前拒绝 oracle-bearing resources/state；
- Runtime dependency graph 不导入 scorer/evaluation；
- Blind CLI 没有 label/scorer 参数或文件访问；
- scorer 拒绝未完成、hash 错误或 digest 不同的 Actual；
- machine label 内部矛盾通过 provenance 标记并排除 Gold 指标，不静默改标签；
- prompt/semantic contract/model identity 变化会改变 Runtime hash/run id。

证据入口：`tests/test_agent2_runtime_blind.py`、`tests/test_agent2_runtime_anti_oracle.py`、`app/agent2/oracle_guard.py`、`docs/ADR/0011-blind-replay-and-sealed-scoring.md`。

## 剩余风险

1. legacy `runtime/replay.py` 仍为编排诊断兼容代码，任何输出固定不得进入 acceptance closure；
2. 1407 条主 reviewer packet 与 168 条 adversarial labels 均为 machine candidates，human approved=0；
3. 精确 842/5562 server corpus 未恢复；
4. Blind actual 能证明无 oracle 与运行事实，不能单独证明 machine labels 业务语义正确。
