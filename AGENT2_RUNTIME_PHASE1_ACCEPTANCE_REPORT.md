# Agent2 Runtime Harness Phase 1 Acceptance Report

> SUPERSEDED: This earlier diagnostic report is retained for provenance only. The final post-review decision and current evidence are in `outputs/AGENT2_RUNTIME_PHASE1_FINAL_ACCEPTANCE_REPORT.md` and `outputs/agent2_runtime_final_acceptance_summary_final2.json`.

生成日期：2026-07-10（Asia/Shanghai）

## 1. Executive verdict

**最终裁决：`NO_GO`**

- **不允许接入生产 Shadow。**
- **不允许进入 Live。**

本轮对现有 687 段/1407 轮 corpus 做出的 replay 在诊断数字上显示 42→0、29→7；但双轴审查确认该 replay 的 semantic adapter 使用 baseline expected/workflow/report delta 生成候选决策，再以同一 baseline 评分，存在 **oracle 泄漏**。因此这些数字只能用于 Planner/Executor 编排诊断，不能证明 42/29 已关闭，Safety 与 Parity 也不得据此通过。根据 Phase 1 Gate 规则，最终严格裁决 `NO_GO`。

未通过的决定性原因：

1. 42/29 closure replay 不是独立语义执行，无法形成可采信的关闭证明。
2. 诊断 replay 仍有 7 个 `copy_previous`，且 Phase 1 无对应 typed command/executor。
3. 14 条独立高风险 semantic tape 候选尚未由独立人工审核，所有语义指标不可成立。
4. 精确的服务器 842 段/5562 轮 raw bundle 与选择 manifest 不在本地。
5. 只发现可写生产 PostgreSQL，无 test/smoke schema，故未执行 DB transaction/rollback smoke。
6. 线上没有部署 Runtime Harness，未形成生产 Shadow 隔离链、kill switch 和 Runtime 日志闭环。

## 2. Git / worktree 状态

- 本地分支：`main`
- 本地基准 HEAD：`36c2199a`
- 工作树：dirty；本轮开始前已存在大量 Agent2、API、workflow、测试及 Runtime 未提交文件。
- 远端生产 HEAD：`96cecec81c4ac9fe4e0500c08a52e661d094147d`
- 远端生产工作树：dirty；`app/agent2/`、`evals/`、多份脚本和测试为未跟踪/未提交状态。
- 本轮未 reset、未覆盖、未删除用户已有修改，未启用生产 Shadow，未写生产 DB。
- 本轮未提交 Git commit：本轮修改与开始时已存在的未提交 Agent2/Runtime 文件重叠，提交会把用户既有修改一并纳入，违反“不得提交用户已有未提交修改”的明确约束。
- 根报告和 `phase1_acceptance/` 仅作为本地验收交付，不作为待提交产物；仓库标准要求输出报告不得提交，本轮遵守该边界。

## 3. 本轮修改文件

行为与 Runtime：

- `app/workflows/action_intake.py`
- `app/agent2/daily_commands.py`
- `app/agent2/cognitive_core_v3.py`
- `app/agent2/command_planner_v3.py`
- `app/agent2/runtime/domains.py`
- `app/agent2/runtime/harness.py`
- `app/agent2/runtime/composition.py`
- `app/agent2/runtime/replay.py`

测试与验收工具：

- `tests/test_agent2_runtime_replay.py`
- `tests/test_agent2_runtime_architecture.py`
- `tests/test_agent2_runtime_harness.py`
- `tests/test_agent2_runtime_acceptance_artifacts.py`
- `scripts/build_agent2_runtime_phase1_acceptance.py`
- `evals/agent2/runtime/phase1_semantic_tape_review_queue.jsonl`
- `evals/agent2/runtime/phase1_acceptance/`
- `REPORT_ISSUE_LEDGER.md`
- `AGENT2_RUNTIME_PHASE1_ACCEPTANCE_REPORT.md`

## 4. 原始 42 个 unexpected write 分类（诊断，未形成关闭证明）

| Root cause | 数量 | 风险 | 处理结果 |
| --- | ---: | --- | --- |
| 案件进展/案件查询被 baseline replay adapter 转成 `capture_daily_event` | 31 | high | oracle-assisted replay 中变为 `record_case_progress` / case command；独立关闭未验证 |
| 出差目标被 baseline replay adapter 转成日报 append | 11 | high | oracle-assisted replay 中变为 travel contract receipt；独立关闭未验证 |
| 合计 | 42 | high | 诊断结果为 0；**验收关闭数 N/A** |

上述分类可用于定位风险，但当前 adapter 直接读取 `expected.should_enter_daily` / `primary_workflow` 来决定 case/travel action，因此“最终 0”不是独立 Core 修复证据。机器台账已统一标记 `evidence_validity=oracle_assisted_diagnostic_only`、`closure_claim_allowed=false`。

## 5. 原始 29 个 expected write miss 分类（诊断，未形成关闭证明）

| Root cause | 原始数量 | 最终状态 |
| --- | ---: | --- |
| 两条合一 delta 未识别为已支持的 `merge_daily_items` | 24 | oracle-assisted replay 生成 `merge_items`；独立语义/目标解析未验证 |
| `copy_previous` 不在 Phase 1 typed contract | 5 | oracle-assisted replay 生成 action + PlanningBlock；仍不执行 |

修复后另有 2 条过去被错误降格为 edit/append 而“碰巧写入”的 copy case 暴露为真实 contract gap。因此最终结果是：

- diagnostic expected write miss：29 → **7**
- 独立验证的已关闭 merge：**N/A**
- diagnostic open copy gap：7（其中 5 个来自原始 29，2 个为修复后新揭示）

## 6. Root cause 与修复层级

- Replay 语义归属：`app/agent2/runtime/replay.py` 可把 baseline expectation 编译成 case/travel/read-only payload；该能力只保留为编排诊断，代码与 summary 已明确禁止将其用于 Safety/Parity acceptance。
- Planner：`record_case_progress` 与 `copy_previous_daily_report` 均产生稳定 PlanningBlock；未实现 case/copy 真实写入。
- Ownership：case action 归 `case`，travel action 归 `travel`，daily merge/copy action 归 `daily`；启动时继续拒绝 owner 冲突。
- Typed contract：merge 使用已有 `merge_items`；copy 保持 unsupported，不绕过 typed executor。
- First-layer：修复 active daily 下“今天汇总月报反馈”的明确日报工作语义，以及日报 + lifestyle 多意图 effect 丢失；保留整轮纯生活问句 no-write。
- 写能力隔离：trusted composition root 只接受仓库内 `InMemoryDailyDomainExecutor` 的精确类型，任意结构相同 adapter 在执行前即被拒绝。
- Audit fail-closed：首次 audit sink 异常稳定返回 `error_code=audit_failed`，不重试同一 sink，也不生成虚假的 `audit_recorded` trace。

所有 ledger root cause 均为明确类别，不存在 `unknown`。

## 7. Independent semantic tape

文件：`evals/agent2/runtime/phase1_semantic_tape_review_queue.jsonl`

- 14 条高风险候选。
- 覆盖误写、危险操作、跨域、多意图、active context、短回复、pending、merge、copy、clear、submit。
- 期望字段包含 goal、entities、segments、owner、action class、executable、clarification、write intent、command/no-write reason 和 risk。
- `annotation_source=codex_machine_proposed_independent_from_runtime_outputs`
- `review_status=pending_human_review`
- human approved：0/14

因此以下指标均为 **N/A / 尚未成立**，没有伪造数值：

- semantic accuracy
- write-intent precision / recall
- executable-action precision
- clarification precision
- domain ownership accuracy
- segmentation accuracy
- high-risk false positives

当前正式 replay 仍固定标记：

- `evaluation_scope=baseline_derived_planner_executor_replay`
- `cognitive_semantic_independence=false`

## 8. Corpus manifest 与缺失情况

Machine-readable manifest：`evals/agent2/runtime/phase1_acceptance/corpus_manifest.json`

程序化 manifest 扫描覆盖 `evals/`、`outputs/`、`data/`、`.tmp_inputs/`、`scripts/`、`docs/` 及可能存在的 `fixtures/`、`corpus/`、`replay/` JSONL；另有独立的 `corpus_search_evidence.json` 记录 `rg -uu`、7 个 ZIP、reachable git history 与绝对路径 `E:\桌面\测试反馈` 的命令和结果。manifest 不再声称 generator 自身扫描了未扫描来源。

已发现的代表性集合：

- 当前 Phase 1：687 段/1407 轮。
- 本地较大结果：747 段/1727 轮。
- 本地组合结果：915 段/3102 轮。
- 服务器历史片段：168 段/1375 轮。

这些集合存在版本重叠、不同 dialogue ID 选择或 result-derived 内容，不能拼接冒充 842/5562。精确 raw bundle、selection window/query、原始文本/状态资源与 SHA-256 manifest 仍缺失。

建议在服务器只读导出后带回：

```bash
PYTHONPATH=. venv/bin/python scripts/run_workflow_gate_replay.py \
  --source all --mode protective_gate --days 60 --limit 10000 \
  --include-text --output /tmp/agent2_phase1_server_history.json
```

导出后仍需证明该文件对应原声明的精确 842/5562 selection，而不是重新取样的另一集合。

## 9. First-layer / Agent2 测试

历史 3 个 first-layer 失败已关闭：

1. active daily 明确工作含“月报”业务对象。
2. 日报 segment + lifestyle segment 的多意图组合。
3. generated realistic active-daily write boundary。

最终 Agent2 + action/workflow 第一层集合：**870 passed in 27.28s**。

未删除、未 skip、未 xfail、未放宽断言；一个旧 replay 测试依据 ADR-0005 更新为直接断言 `cognitive_semantic_independence=false`，不再通过故意制造 semantic mismatch 表达“不独立”。

## 10. 全量 pytest

```text
1156 passed, 142 failed in 31.91s
```

142 个失败集中于既有 Agent Core snapshot wording 与 `test_report_agent_state_protocol.py` 旧状态协议。Agent2/first-layer 指定集合为 870/870 全绿。本轮未处理这些不属于 Runtime Phase 1 的 legacy 失败，也未以恢复 fallback 的方式消除它们。

## 11. Runtime replay（oracle-assisted diagnostic）

最终命令：

```powershell
.\venv\Scripts\python.exe scripts\replay_agent2_runtime_harness.py `
  --manifest evals\agent2\runtime\phase1_inputs.json `
  --output-dir outputs\agent2_runtime_phase1_acceptance_replay
```

最终结果：

| 指标 | 修复前 | 修复后 |
| --- | ---: | ---: |
| dialogues / turns | 687 / 1407 | 687 / 1407 |
| mismatches | 876 | 582 |
| unexpected write intent | 42 | **0** |
| expected write miss | 29 | **7** |
| actual write | 0 | 0 |
| legacy fallback | 0 | 0 |
| typed executor bypass | 0 | 0 |
| diagnostic execution invariants | false | true |
| acceptance_eligible | false | **false** |
| safety_ready | false | **false** |
| parity_ready | false | false |

剩余 582 个 diff：plan 560、semantic 174、execution 68、reply 3（同一 turn 可有多个 stage diff）；其中 7 个 write-intent diff 均为 copy contract gap。0 actual-write、0 fallback、0 typed bypass 是模拟执行链的诊断证据；因 semantic decision 来自评分 oracle，不能推出 Safety Gate PASS。

## 12. DB smoke

隔离 executor/Runtime contract smoke 覆盖 dry-run actual-write=0、typed validation、owner、version、idempotency、raw-text seam、unknown schema、receipt、failure no-fallback，以及 trusted composition 与 audit fail-closed。最终通过数量见机器证据 `test_evidence.json` / `smoke_evidence.json`。

真实 DB integration smoke：**BLOCKED / 未通过**。

- 发现 PostgreSQL 数据库 `ai_review`。
- server `read_only=false`。
- test/smoke-like schema count=0。
- 仅有生产 `.env`/systemd EnvironmentFile；未发现隔离数据库。

因此没有执行 typed command 事务写、rollback、DB idempotency、DB optimistic-lock 或 receipt-vs-effect 测试。这样做遵守“不写生产数据库”的禁令，但 Operational Gate 不能通过。

## 13. Online read-only smoke

线上只读检查：

- `GET /health`：`{"status":"ok"}`。
- `ai-review-api.service`：active。
- `ai-review-stream.service`：active。
- `ai-review-scheduler.service`：active。
- OpenAPI 存在 `/reports/manual`、`/webhooks/dingtalk`、`/health`。
- `app/api/reports.py`、`app/api/webhook.py`、`app/stream_runner.py` 与本地哈希一致。

决定性失败：

- 线上不存在 `app/agent2/runtime/harness.py`、`app/agent2/runtime/replay.py`。
- 线上 Cognitive Core / Planner 与本地哈希不同。
- 当前三入口仍直接编排 cognitive v3 / daily shadow / legacy 链，不是 Phase 1 Runtime Harness。
- 没有可确认的 Runtime Shadow kill switch、run-id 日志定位、Conversation State non-persistence 或 Shadow/Live 物理隔离在线证据。

本轮未调用任何 POST smoke，未修改线上状态，未写生产库。

## 14. 未关闭 blocker

1. 42/29 anomaly closure 尚无独立语义 replay 证明；当前 0/7 仅为 oracle-assisted diagnostic。
2. 7 个 `copy_previous` typed contract/executor gap。
3. 14 条 semantic tape 未人工审核，独立语义指标不可用。
4. 842/5562 corpus 与 manifest 缺失。
5. baseline-derived replay 仍有 582 个 diff，且 acceptance-eligible=false。
6. 无隔离 DB，DB integration smoke 未执行。
7. Runtime Harness 未部署线上，online Operational Gate 失败。
8. Runtime Shadow kill switch、日志定位、状态不持久化、零业务写和 Live 物理隔离未获得在线证据。

## 15. Shadow Go / No-Go checklist

| Gate | 条件 | 结果 |
| --- | --- | --- |
| Safety（现有 687/1407 replay） | 独立 semantic source + unexpected high-risk write=0 | **FAIL：oracle-assisted** |
| Safety | legacy fallback=0、trusted simulator、audit fail-closed | PASS（结构/隔离测试子项） |
| Safety | typed bypass/receipt/owner/schema guard | PASS（隔离测试子项；不等于整 Gate） |
| Semantic | 独立人工 Gold + 分项指标 | **FAIL** |
| Semantic | 42 unexpected write 独立关闭 | **FAIL：未验证** |
| Semantic | 29 expected miss 全关闭/错误标注证明 | **FAIL：未验证，诊断仍有 7 copy gap** |
| Parity | first-layer / Agent2 tests | PASS：870/870 |
| Parity | full 842/5562 corpus | **FAIL：缺失** |
| Parity | `parity_ready` | **FAIL：582 mismatch** |
| Operational | DB smoke | **FAIL：无隔离目标** |
| Operational | online read-only Runtime smoke | **FAIL：Runtime 未部署** |
| Operational | Shadow state/write/Live isolation/kill switch/log | **FAIL：无在线证据** |

## 16. Final decision

`NO_GO`

- Production Shadow：**不允许**。
- Live：**不允许**。

下一步最小动作：由独立人工 reviewer 审核并签核 `phase1_semantic_tape_review_queue.jsonl` 的 14 条候选；随后必须让 Runtime 的真实 semantic interpreter 仅从 raw text/state/resources 读取这些样本，禁止读取 expected/baseline，再运行 replay。只有这一条独立链能开始验证 42/29 closure；之后仍需关闭 copy contract、恢复 842/5562 corpus、建立隔离 DB 并部署只读 Runtime Shadow。

## 可复现命令

Artifact：

```powershell
.\venv\Scripts\python.exe scripts\build_agent2_runtime_phase1_acceptance.py
```

Agent2 + first-layer：

```powershell
$files = @(Get-ChildItem tests -Filter 'test_agent2*.py')
$files += @(
  'tests\test_action_intake.py',
  'tests\test_action_context.py',
  'tests\test_workflow_intake.py',
  'tests\test_semantic_router_guards.py',
  'tests\test_workflow_replay_daily_context.py'
)
.\venv\Scripts\python.exe -m pytest $files -q
```

全量：

```powershell
.\venv\Scripts\python.exe -m pytest -q --tb=no
```

Smoke 证据与精确命令见：

- `evals/agent2/runtime/phase1_acceptance/smoke_evidence.json`
- `evals/agent2/runtime/phase1_acceptance/test_evidence.json`
- `evals/agent2/runtime/phase1_acceptance/corpus_search_evidence.json`

Machine-readable 交付：

- `evals/agent2/runtime/phase1_acceptance/case_ledger.jsonl`
- `evals/agent2/runtime/phase1_acceptance/corpus_manifest.json`
- `evals/agent2/runtime/phase1_acceptance/semantic_tape_manifest.json`
- `evals/agent2/runtime/phase1_acceptance/acceptance_summary.json`
