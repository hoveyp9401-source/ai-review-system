# Agent Core P0/P1 Baseline

Status: active baseline, 2026-07-03.

## P0: Project Baseline

P0 is not a feature phase. It is the control layer that prevents Agent2 work
from drifting back into another large daily-report patch stack.

### In Scope Today

- Keep production daily-report writes unchanged unless a later explicit release
  gate enables Agent Core execution.
- Add a single planning baseline for Agent Core work:
  - `workflows/legal-collaboration-agent-roadmap.md`
  - this file
- Define the current test gate before any gray release.
- Create a minimal Agent Core dry-run interface that can be tested without
  DingTalk and without database writes.

### Out Of Scope Today

- No DingTalk channel refactor.
- No real Agent Core write enablement.
- No vector database.
- No legal-research production workflow.
- No automatic travel or case notifications.
- No cleanup of user or generated files outside the Agent Core work area.

### Current Production Boundary

Existing production behavior stays owned by the current daily-report and monthly
report paths. Agent Core P1 must be observe-only or dry-run.

Agent Core may call existing planning and snapshot functions, but it must not:

- send DingTalk messages;
- write a daily report;
- submit a monthly report;
- notify travelers;
- update case records.

## P1: Agent Core Observe-Only Minimum

P1 is complete only when one public interface can process a text turn and return
an explainable dry-run result.

### Required Interface

```text
process_agent_turn(envelope, daily_snapshot=None) -> AgentTurnResult
```

The result must contain:

- turn id;
- action plan;
- routing plan;
- coordination plan;
- execution policy;
- daily commands, when daily is authorized;
- operation ledger entries;
- before snapshot;
- after snapshot;
- no production write.

### Required Behavior

1. Daily text can produce a dry-run daily operation and before/after state.
2. Non-daily text cannot produce daily write operations.
3. Travel text can produce a sandbox side effect while still allowing a daily
   tomorrow-plan write when the first layer authorizes both.
4. A stale downstream coordination daily action cannot bypass the routing plan.
5. The result must be serializable for harness reports.

### P1 Test Gate

Run at least:

```powershell
python -m pytest tests/test_agent_core_processor.py
python -m pytest tests/test_agent2_daily_shadow.py tests/test_agent2_daily_commands.py
```

Before any gray release, also run the broader Agent2 harness and online shadow
tests. P1 alone does not mean gray-ready.

## What Counts As Not Done

- A passing router-only test is not enough.
- A new heuristic in `report_service.py` is not P1.
- A doc without executable tests is not P1.
- A test count without before/after and ledger visibility is not P1.
- Any path that lets coordination or daily extraction write without routing
  authorization fails P1.

## 2026-07-03 P0/P1 Result

### Completed

- Added this P0/P1 baseline.
- Added Agent Core dry-run interface:
  - `app/agent_core/__init__.py`
  - `app/agent_core/processor.py`
- Added behavior tests:
  - `tests/test_agent_core_processor.py`
- The new interface returns:
  - action plan;
  - routing plan;
  - coordination plan;
  - execution policy;
  - daily commands;
  - before/after daily snapshot;
  - operation ledger;
  - `production_write=False`.
- Fixed action-first routing for:
  - generic case/project work as daily work;
  - specific case work as daily work plus case-progress sandbox;
  - structured weekly report not entering daily;
  - current travel mention as daily work plus travel sandbox.

### Verification

Commands run successfully:

```powershell
python -m pytest tests/test_agent_core_processor.py tests/test_agent2_daily_shadow.py tests/test_agent2_daily_commands.py -q
# 41 passed

$files = Get-ChildItem tests -Filter 'test_agent2_*.py' | ForEach-Object { $_.FullName }
python -m pytest @files tests/test_action_intake.py tests/test_workflow_intake.py tests/test_workflow_gate.py tests/test_workflow_replay_daily_context.py -q
# 251 passed

python scripts\eval_agent2_harness.py --fail-on-severity high
# 29 total, 29 passed, 0 failed, 0 unexpected failures
```

One broader collection command failed because the local environment is not a
clean full-test environment:

- `asyncpg` is missing locally;
- `app.agent.edit_cursor` is referenced by older tests but is not present in
  this local tree.

That failure is not counted as a product pass or fail for P1. It is a P0
environment/governance item to resolve before claiming full-suite readiness.

### Not Yet Claimed

- No production write path is enabled.
- No DingTalk channel behavior changed.
- No gray release is approved.
- No database-backed OperationLedger table has been added.
- No full local test suite pass is claimed.

## P2: DailyCapability Dry-Run

P2 turns daily report from processor-local execution into an Agent Core
capability module. The production write path is still disabled.

### Required Interface

```text
run_daily_capability(turn_id, snapshot, commands, previous_snapshot=None) -> DailyCapabilityResult
```

The result must contain:

- before and after daily snapshot;
- applied daily commands;
- command action results;
- operation ledger entries;
- stable item refs before and after execution;
- `changed` and `read_only` flags.

### Required Behavior

1. Expose stable global item numbering across:
   - today work;
   - problems/risks;
   - tomorrow plan.
2. Support field-local and global item edits through existing daily execution
   logic.
3. Preserve item id visibility for delete and merge actions.
4. Support revoke and copy-previous in dry-run mode.
5. Keep processor as an orchestrator; daily execution lives in
   `DailyCapability`.

## 2026-07-04 P2 Result

### Completed

- Added DailyCapability module:
  - `app/agent_core/daily_capability.py`
  - `app/agent_core/types.py`
- Refactored processor to call `run_daily_capability(...)` instead of applying
  daily commands inline.
- Exported the capability from `app/agent_core/__init__.py`.
- Added capability tests:
  - `tests/test_agent_core_daily_capability.py`
- Added processor assertion that the Agent Core result exposes
  `daily_capability.after_items`.

### Verification

Commands run successfully:

```powershell
python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py -q
# 12 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent2_daily_execution.py tests/test_agent2_daily_commands.py tests/test_agent2_daily_shadow.py -q
# 84 passed

$files = Get-ChildItem tests -Filter 'test_agent2_*.py' | ForEach-Object { $_.FullName }
python -m pytest @files tests/test_action_intake.py tests/test_workflow_intake.py tests/test_workflow_gate.py tests/test_workflow_replay_daily_context.py -q
# 251 passed

python scripts\eval_agent2_harness.py --fail-on-severity high
# 29 total, 29 passed, 0 failed, 0 unexpected failures
```

### Not Yet Claimed

- No production daily-report write path is enabled.
- No DingTalk behavior changed.
- No database-backed OperationLedger table has been added.
- No full local test suite pass is claimed because the P0 environment issues
  remain unresolved.

## P2.5: ExecutionPolicy Authorization Boundary

P2.5 adds a real execution authorization seam between workflow routing and
capability execution. This is the guardrail that prevents Agent2 from drifting
back into Agent1-style behavior where a downstream extractor can write daily
content just because it guessed a field.

### Required Interface

```text
build_execution_policy(turn_id, routing_plan) -> ExecutionPolicy
authorize_capability_request(policy, turn_id, capability, operation, ...) -> AuthorizationDecision
```

The policy must contain turn-scoped authorization tickets. A capability may only
execute an operation when the ticket matches:

- turn id;
- capability;
- operation;
- target field, when applicable;
- task id, when applicable;
- write policy;
- expiry time.

### Required Behavior

1. DailyCapability must reject write commands when no authorization exists.
2. Field overreach must be rejected, for example a `today_work` grant cannot
   write `problems`.
3. Expired authorization tickets must be rejected.
4. Sandbox authorization cannot be used as a commit-like write.
5. Sidecar workflows such as travel coordination and case progress remain
   sandbox-only unless explicitly authorized.
6. Coordination actions alone cannot authorize writes. Authorization comes from
   `RoutingPlan.effects`.

## 2026-07-04 P2.5 Result

### Completed

- Added ExecutionPolicy module:
  - `app/agent_core/execution_policy.py`
- Updated DailyCapability so write commands require a matching authorization
  ticket.
- Updated Agent Core processor to build authorization tickets from
  `RoutingPlan.effects`.
- Added authorization metadata to operation ledger entries:
  - `plan_id`;
  - `authorization_id`;
  - `authorization_status`.
- Added sidecar authorization checks for:
  - travel coordination sandbox entries;
  - case progress sandbox entries.
- Added regression coverage proving a downstream coordination daily action
  cannot write without a first-layer routing effect.

### Verification

Commands run successfully:

```powershell
python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py -q
# 19 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent2_daily_execution.py tests/test_agent2_daily_commands.py tests/test_agent2_daily_shadow.py -q
# 91 passed

$files = Get-ChildItem tests -Filter 'test_agent2_*.py' | ForEach-Object { $_.FullName }
python -m pytest @files tests/test_action_intake.py tests/test_workflow_intake.py tests/test_workflow_gate.py tests/test_workflow_replay_daily_context.py tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py -q
# 270 passed

python scripts\eval_agent2_harness.py --fail-on-severity high
# 29 total, 29 passed, 0 failed, 0 unexpected failures
```

Manual sample checks:

- `明天去南京出差`:
  - daily tomorrow-plan write authorized as `dry_run`;
  - travel coordination authorized as `sandbox`;
  - no production write.
- legal Q&A only:
  - no daily authorization;
  - no daily operation ledger.
- specific case plus daily work:
  - daily work authorized as `dry_run`;
  - case progress authorized as `sandbox`.

### Not Yet Claimed

- No production write path is enabled.
- No DingTalk behavior changed.
- No database-backed OperationLedger table has been added.
- No gray release is approved.
- No full local test suite pass is claimed because the P0 environment issues
  remain unresolved.

Full-suite probe:

```powershell
python -m pytest -q
# collection failed before running the full suite
```

The current repo root still collects historical or temporary test locations
such as `.tmp_*`, `remote_edit/`, and `server_patch/`. The local environment
also lacks dependencies used by older production tests, including `asyncpg`,
`tenacity`, and `dingtalk_stream`, while several old tests still reference
`app.agent.edit_cursor`, which is absent in this local tree. This means P2.5 is
validated by the targeted Agent2/Agent Core gates above, not by a clean
repository-wide test pass.

## P2.6: TaskLedger v0

P2.6 adds a task-context seam before routing. The goal is to stop treating every
turn as a fresh one-message classification problem. Agent Core can now ask a
TaskLedger adapter what active task facts exist for the sender, then pass those
facts to the existing routing layer as `ActiveWorkflowTask` context.

This is still dry-run only. It does not persist tasks to the database and does
not change DingTalk production behavior.

### Required Interface

```text
TaskLedgerEntry(...)
InMemoryTaskLedger(entries).active_entries_for_user(user_id)
apply_task_ledger_context(envelope, task_ledger) -> (envelope, TaskLedgerContext)
process_agent_turn(..., task_ledger=ledger)
```

The v0 ledger entry records:

- `task_id`;
- `user_id`;
- `workflow`;
- `status`;
- `awaited_reply`;
- `prompt`;
- `authorization`;
- `artifacts`;
- `linked_task_ids`;
- metadata and timestamps.

### Required Behavior

1. A monthly-report task waiting for metric replies can claim metric reply text.
2. A daily-report task waiting for tomorrow-plan text can claim a future travel
   plan while travel coordination remains a sandbox sidecar.
3. A daily-report task waiting for confirmation can claim confirmation text.
4. A daily-report task waiting for follow-up text can keep edit commands such
   as `把第1条改成...` in the daily context.
5. A monthly task must not steal a legal Q&A turn just because it is active.
6. Concurrent daily and monthly tasks are selected by awaited reply match, not
   by fixed workflow priority.

## 2026-07-04 P2.6 Result

### Completed

- Added TaskLedger module:
  - `app/agent_core/task_ledger.py`
- Added TaskLedger context to Agent Core result:
  - `task_context`;
  - selected task id/workflow;
  - active tasks derived from the ledger;
  - considered task facts.
- Updated `process_agent_turn(...)` to accept `task_ledger=...` and enrich the
  envelope before action intake, workflow routing, coordination planning, and
  execution authorization.
- Kept the existing `ActiveWorkflowTask` interface as the router seam instead
  of replacing the router with another task-specific rule stack.
- Added TaskLedger behavior tests:
  - `tests/test_agent_core_task_ledger.py`.

### Verification

Commands run successfully:

```powershell
python -m pytest tests/test_agent_core_task_ledger.py -q
# 5 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py -q
# 24 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py tests/test_agent2_daily_execution.py tests/test_agent2_daily_commands.py tests/test_agent2_daily_shadow.py -q
# 96 passed

$files = Get-ChildItem tests -Filter 'test_agent2_*.py' | ForEach-Object { $_.FullName }
python -m pytest @files tests/test_action_intake.py tests/test_workflow_intake.py tests/test_workflow_gate.py tests/test_workflow_replay_daily_context.py tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py -q
# 275 passed

python scripts\eval_agent2_harness.py --fail-on-severity high
# 29 total, 29 passed, 0 failed, 0 unexpected failures
```

Manual sample checks with concurrent monthly and daily tasks:

- monthly metric reply:
  - selected task: monthly task;
  - route: `monthly_report`;
  - no daily write.
- `明天去南京出差`:
  - selected task: daily task;
  - route: `travel_coordination` plus `daily_report`;
  - daily tomorrow-plan dry-run write;
  - travel sidecar remains sandbox.
- legal Q&A:
  - no selected task;
  - route: `internal_qa`;
  - no daily write.

### Not Yet Claimed

- No production write path is enabled.
- No DingTalk behavior changed.
- No database-backed TaskLedger table has been added.
- No database-backed OperationLedger table has been added.
- No gray release is approved.
- No full local test suite pass is claimed because the P0 environment issues
  remain unresolved.

## P2.7: Persistable OperationLedger v0

P2.7 turns the in-memory `OperationLedgerEntry` list into JSON-safe records
that can be written by a persistence adapter. The Agent Core default path still
does not persist anything; persistence only happens when an explicit
`operation_ledger_store` adapter is supplied.

This keeps the core dry-run and testable while preparing the database seam for
later production wiring.

### Required Interface

```text
build_persistable_operation_records(turn_result) -> list[PersistableOperationRecord]
InMemoryOperationLedgerStore().upsert_many(records)
process_agent_turn(..., operation_ledger_store=store)
```

The storage record includes:

- `record_schema`;
- `operation_id`;
- `turn_id`;
- `workflow`;
- `capability`;
- `operation`;
- `write_policy`;
- `plan_id`;
- `task_id`;
- `authorization_id`;
- `authorization_status`;
- `before_snapshot`;
- `after_snapshot`;
- `auth_chain`;
- `result`;
- `safety_flags`;
- `reason`;
- `created_at`.

### Required Behavior

1. Daily operations and sidecar operations can both become persistable records.
2. Denied authorization is persisted as a blocked operation with safety flags.
3. Records are JSON-serializable and do not store raw user text.
4. Store writes are idempotent by `operation_id`.
5. Records can be queried by turn id and selected task id.
6. Supplying a store does not enable production writes.

## 2026-07-04 P2.7 Result

### Completed

- Added OperationLedger persistence module:
  - `app/agent_core/operation_ledger.py`
- Added explicit storage record schema:
  - `agent_core_operation_ledger.v1`
- Added in-memory adapter:
  - `InMemoryOperationLedgerStore`
- Updated Agent Core processor with optional persistence adapter:
  - `process_agent_turn(..., operation_ledger_store=store)`
- Added persisted record visibility to `AgentTurnResult.as_dict()`.
- Exported OperationLedger helpers from `app/agent_core/__init__.py`.
- Added OperationLedger behavior tests:
  - `tests/test_agent_core_operation_ledger.py`.

### Verification

Commands run successfully:

```powershell
python -m pytest tests/test_agent_core_operation_ledger.py -q
# 5 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py tests/test_agent_core_operation_ledger.py -q
# 29 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py tests/test_agent_core_operation_ledger.py tests/test_agent2_daily_execution.py tests/test_agent2_daily_commands.py tests/test_agent2_daily_shadow.py -q
# 101 passed

$files = Get-ChildItem tests -Filter 'test_agent2_*.py' | ForEach-Object { $_.FullName }
python -m pytest @files tests/test_action_intake.py tests/test_workflow_intake.py tests/test_workflow_gate.py tests/test_workflow_replay_daily_context.py tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py tests/test_agent_core_operation_ledger.py -q
# 280 passed

python scripts\eval_agent2_harness.py --fail-on-severity high
# 29 total, 29 passed, 0 failed, 0 unexpected failures
```

### Not Yet Claimed

- No production write path is enabled.
- No DingTalk behavior changed.
- No database-backed OperationLedger table has been added.
- No database-backed TaskLedger table has been added.
- No gray release is approved.
- No full local test suite pass is claimed because the P0 environment issues
  remain unresolved.

## P3: MonthlyCapability v0

P3 makes monthly-report collection a first-class Agent Core capability. It no
longer relies only on a guardrail that prevents monthly text from entering the
daily-report path. A routed monthly reply now becomes a monthly command, is
authorized by the first-layer routing plan, mutates only a monthly snapshot in
dry-run mode, and writes an operation ledger entry.

This keeps the product direction aligned with "capability first, workflow
owned, no production side effects".

### Required Interface

```text
MonthlySnapshot(metrics=[MonthlyMetricState(...)])
MonthlyCommand(operation="capture_reply" | "confirm_submission")
run_monthly_capability(turn_id, snapshot, commands, execution_policy)
process_agent_turn(..., monthly_snapshot=snapshot)
```

The monthly snapshot stores:

- task id;
- department;
- leader;
- period label;
- status;
- metric number/name/unit;
- unfinished reason / existing problem;
- next-month target;
- action plan.

### Required Behavior

1. A monthly metric reply is rejected without a matching authorization token.
2. One message can fill multiple metrics.
3. Later messages can fill the remaining metrics without overwriting earlier
   metrics.
4. A specific metric field can be edited after preview.
5. `确认提交` only confirms a monthly task when the TaskLedger says that task is
   awaiting monthly confirmation.
6. Monthly replies do not create daily-report commands.
7. A monthly active task does not steal a legal Q&A turn.
8. Monthly operation ledger entries include the auth chain and no production
   write path.

## 2026-07-04 P3 Result

### Completed

- Added monthly capability module:
  - `app/agent_core/monthly_capability.py`
- Added monthly command/snapshot/result types:
  - `MonthlyCommand`;
  - `MonthlyMetricState`;
  - `MonthlySnapshot`;
  - `MonthlyCapabilityResult`.
- Updated Agent Core processor:
  - `process_agent_turn(..., monthly_snapshot=snapshot)`;
  - compiles `capture_monthly_report_reply` into `capture_reply`;
  - compiles `confirm_monthly_report_submission` into `confirm_submission`;
  - returns `monthly_before`, `monthly_after`, and `monthly_capability`.
- Updated execution authorization:
  - monthly-report effects get dry-run authorization;
  - MonthlyCapability refuses to mutate without that authorization.
- Tightened TaskLedger confirmation handling:
  - confirmation tasks set `awaiting_confirmation`;
  - they no longer also masquerade as ordinary reply candidates.
- Added monthly behavior tests:
  - `tests/test_agent_core_monthly_capability.py`.

### Verification

Commands run successfully:

```powershell
python -m pytest tests/test_agent_core_monthly_capability.py -q
# 7 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py tests/test_agent_core_operation_ledger.py tests/test_agent_core_monthly_capability.py -q
# 36 passed

python -m pytest tests/test_action_intake.py tests/test_workflow_intake.py tests/test_workflow_gate.py tests/test_agent2_active_daily_context_guardrails.py tests/test_agent2_coordination_plan.py tests/test_agent2_daily_commands.py tests/test_agent2_daily_execution.py tests/test_agent2_daily_execution_guardrails.py tests/test_agent2_daily_execution_replay.py tests/test_agent2_golden_cases.py tests/test_agent2_dialogue_replay.py tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py tests/test_agent_core_operation_ledger.py tests/test_agent_core_monthly_capability.py -q
# 225 passed

$files = Get-ChildItem tests -Filter '*.py' | Where-Object { $_.Name -match '^(test_agent2_|test_workflow_|test_action_intake|test_daily_context|test_daily_intent_protocol|test_agent_core_)' } | ForEach-Object { $_.FullName }
python -m pytest $files -q
# 296 passed

python scripts\eval_agent2_harness.py --cases evals\agent2\golden --output-dir outputs\agent2_harness_monthly_capability_p3 --gate-mode protective_gate --fail-on-severity low
# 29 total, 29 passed, 0 failed, 0 unexpected failures

python -m py_compile app\agent_core\monthly_capability.py app\agent_core\processor.py app\agent_core\execution_policy.py app\agent_core\task_ledger.py
# passed
```

Safety check:

```powershell
rg "send|dingtalk|commit\(|create_async_engine|AsyncSession|PerformanceSubmission|session\." app\agent_core -n
# no production send/db write paths; only sender/dingtalk id fields and ledger lookup references matched
```

### Not Yet Claimed

- No production write path is enabled.
- No DingTalk behavior changed.
- No database-backed MonthlyCapability table has been added.
- No database-backed OperationLedger table has been added.
- No database-backed TaskLedger table has been added.
- Monthly parsing is v0 and intentionally snapshot-based; it does not yet parse
  authoritative Excel/Word metric source files in Agent Core.
- No gray release is approved.
- No full local test suite pass is claimed because the P0 environment issues
  remain unresolved.

## P4: TravelCapability Sandbox v0

P4 promotes travel coordination from a generic sidecar ledger entry into a
first-class sandbox capability. It still does not notify anyone and does not
write any official travel table. It only creates auditable `TravelPlanArtifact`
objects and optional `TravelOverlap` hints.

This is the correct next step for the broader legal collaboration system:
daily report remains the user-visible draft workflow, while travel coordination
becomes an independent capability that can coexist with daily entries.

### Required Interface

```text
TravelPlanArtifact(...)
TravelOverlap(...)
run_travel_capability(turn_id, envelope, coordination, existing_plans, execution_policy)
process_agent_turn(..., existing_travel_plans=[...])
```

The travel plan artifact stores:

- traveler id/name;
- destination;
- date hint;
- trip status: planned, tentative, already_traveled;
- activity hint;
- return uncertainty flag;
- source hash/chars;
- confidence;
- notification/write switches, both disabled.

### Required Behavior

1. A travel candidate is rejected without a matching sandbox authorization.
2. Future travel can produce both:
   - a daily tomorrow-plan item;
   - a travel sandbox candidate.
3. Product work about "出差协同系统" must not become a real trip candidate.
4. Already-happened travel can be marked `already_traveled`.
5. Return uncertainty can be recorded without prompting or notifying.
6. Same destination and same date hint can create an overlap hint.
7. The Agent Core operation ledger records the travel capability once, not both
   as TravelCapability and as the old generic sidecar.

## 2026-07-04 P4 Result

### Completed

- Added travel capability module:
  - `app/agent_core/travel_capability.py`
- Added sandbox travel structures:
  - `TravelPlanArtifact`;
  - `TravelOverlap`;
  - `TravelCapabilityResult`.
- Updated Agent Core processor:
  - exposes `travel_capability`;
  - accepts `existing_travel_plans=...`;
  - routes travel actions through TravelCapability;
  - skips duplicate generic sidecar entries for travel actions.
- Added travel behavior tests:
  - `tests/test_agent_core_travel_capability.py`.

### Verification

Commands run successfully:

```powershell
python -m pytest tests/test_agent_core_travel_capability.py -q
# 5 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py tests/test_agent_core_operation_ledger.py tests/test_agent_core_monthly_capability.py tests/test_agent_core_travel_capability.py -q
# 41 passed

$files = Get-ChildItem tests -Filter '*.py' | Where-Object { $_.Name -match '^(test_agent2_|test_workflow_|test_action_intake|test_daily_context|test_daily_intent_protocol|test_agent_core_)' } | ForEach-Object { $_.FullName }
python -m pytest $files -q
# 301 passed

python scripts\eval_agent2_harness.py --cases evals\agent2\golden --output-dir outputs\agent2_harness_travel_capability_p4 --gate-mode protective_gate --fail-on-severity low
# 29 total, 29 passed, 0 failed, 0 unexpected failures

python -m py_compile app\agent_core\travel_capability.py app\agent_core\processor.py app\agent_core\execution_policy.py
# passed
```

Safety check:

```powershell
rg "send|dingtalk|commit\(|create_async_engine|AsyncSession|PerformanceSubmission|session\.|notification_enabled=True|official_write_enabled=True" app\agent_core -n
# no production send/db write/notification paths; only sender/dingtalk id fields and sandbox traveler fields matched
```

### Not Yet Claimed

- No DingTalk private reminder is enabled.
- No official travel-plan table write is enabled.
- No travel spreadsheet ingestion is implemented in Agent Core yet.
- Overlap detection is v0 and only compares same destination plus same date
  hint; it does not yet resolve date ranges or business substitutability.
- No gray release is approved.
- No full local test suite pass is claimed because the P0 environment issues
  remain unresolved.

## P5: CaseProgressCapability Sandbox v0

P5 gives case progress its own sandbox capability. It is intentionally narrow:
only a coordination action already recognized as a concrete matter can become a
case-progress candidate. Legal Q&A and generic "案件沟通" text must not become
case progress.

This keeps the first-layer intent ownership intact and prevents the system from
falling back into "everything is a daily report or case update" behavior.

### Required Interface

```text
CaseRecord(case_id, matter_hint, owner_id, owner_name)
CaseProgressItem(...)
run_case_progress_capability(turn_id, envelope, coordination, case_records, execution_policy)
process_agent_turn(..., case_records=[...])
```

The case progress item stores:

- matter hint;
- reporter id/name;
- optional matched case id;
- optional owner id/name;
- source hash/chars;
- confidence;
- notification/write switches, both disabled.

### Required Behavior

1. A case-progress candidate is rejected without matching sandbox
   authorization.
2. Specific matter progress can create:
   - a daily entry;
   - a case-progress sandbox candidate.
3. A known case record can be linked by matter hint.
4. Generic "案件沟通" stays daily-only.
5. Legal Q&A stays Q&A and does not create a case-progress candidate.
6. The Agent Core operation ledger records the case-progress capability once,
   not both as CaseProgressCapability and as the old generic sidecar.

## 2026-07-04 P5 Result

### Completed

- Added case progress capability module:
  - `app/agent_core/case_progress_capability.py`
- Added sandbox case structures:
  - `CaseRecord`;
  - `CaseProgressItem`;
  - `CaseProgressCapabilityResult`.
- Updated Agent Core processor:
  - exposes `case_progress_capability`;
  - accepts `case_records=...`;
  - routes case-progress actions through CaseProgressCapability;
  - skips duplicate generic sidecar entries for case-progress actions.
- Added case-progress behavior tests:
  - `tests/test_agent_core_case_progress_capability.py`.

### Verification

Commands run successfully:

```powershell
python -m pytest tests/test_agent_core_case_progress_capability.py -q
# 5 passed

python -m pytest tests/test_agent_core_processor.py tests/test_agent_core_daily_capability.py tests/test_agent_core_execution_policy.py tests/test_agent_core_task_ledger.py tests/test_agent_core_operation_ledger.py tests/test_agent_core_monthly_capability.py tests/test_agent_core_travel_capability.py tests/test_agent_core_case_progress_capability.py -q
# 46 passed

$files = Get-ChildItem tests -Filter '*.py' | Where-Object { $_.Name -match '^(test_agent2_|test_workflow_|test_action_intake|test_daily_context|test_daily_intent_protocol|test_agent_core_)' } | ForEach-Object { $_.FullName }
python -m pytest $files -q
# 306 passed

python scripts\eval_agent2_harness.py --cases evals\agent2\golden --output-dir outputs\agent2_harness_case_progress_p5 --gate-mode protective_gate --fail-on-severity low
# 29 total, 29 passed, 0 failed, 0 unexpected failures

python -m py_compile app\agent_core\case_progress_capability.py app\agent_core\travel_capability.py app\agent_core\monthly_capability.py app\agent_core\processor.py app\agent_core\execution_policy.py
# passed
```

Safety check:

```powershell
rg "send|dingtalk|commit\(|create_async_engine|AsyncSession|PerformanceSubmission|session\.|notification_enabled=True|official_write_enabled=True" app\agent_core -n
# no production send/db write/notification paths; only sender/dingtalk id fields and sandbox reporter/traveler fields matched
```

### Not Yet Claimed

- No official case-progress table write is enabled.
- No DingTalk case owner reminder is enabled.
- CaseRecord matching is v0 matter-hint matching only.
- No case ledger import from authoritative plaintiff/defendant workbook has
  been implemented in Agent Core yet.
- No gray release is approved.
- No full local test suite pass is claimed because the P0 environment issues
  remain unresolved.

## 2026-07-04 Real Replay And Smoke

After P3-P5, Agent2 was replayed against both real server-history dialogues and
the labeled smoke dialogue set.

### Real History Replay

Input:

- `evals/agent2/history_dialogues/server_history_60d.jsonl`
- 168 real dialogues
- 1375 real user turns

Dialogue replay:

```powershell
python scripts\replay_agent2_dialogues.py --input evals\agent2\history_dialogues\server_history_60d.jsonl --output-dir outputs\agent2_real_history_dialogue_replay_20260704 --gate-mode protective_gate
```

Result:

- passed dialogues: 168 / 168
- mismatches: 0
- sandbox official writes: 0
- sandbox notifications: 0
- travel candidates: 33
- case-progress candidates: 29

The dialogue-level risk flags were mostly expected dry-run/write-context flags:

- `dry_run_write_impact`: 878
- `had_daily_context`: 750
- `cross_turn_daily_write`: 37

Daily execution replay:

```powershell
python scripts\replay_agent2_daily_execution.py evals\agent2\history_dialogues\server_history_60d.jsonl --output-dir outputs\agent2_real_history_daily_execution_20260704 --gate-mode protective_gate
```

Result:

- dialogues: 168
- turns: 1375
- failed: 0
- mismatches: 0
- risk turns: 0
- fallback to legacy: 0
- unexpected direct writes: 0
- gray ready: true

### Labeled Smoke Replay

Input:

- `evals/agent2/dialogues`
- 161 labeled smoke dialogues
- 760 labeled turns

Before final rerun, seven stale expectations were updated:

- `copy_previous_variants`: after copying yesterday's report, `确认提交` is now
  expected to confirm the active daily draft instead of asking an orphan
  clarification.
- `completed_previous_plan_*`: expected command is now
  `complete_previous_plan`, not generic `fill`.
- `self_generated_copy_confirm_revoke`: "昨天的明日计划已完成" now expects the
  previous tomorrow-plan item to be expanded into today's completed work.

Final dialogue smoke:

```powershell
python scripts\replay_agent2_dialogues.py --input evals\agent2\dialogues --output-dir outputs\agent2_smoke_dialogues_20260704_final --gate-mode protective_gate --fail-on-mismatch
```

Result:

- passed dialogues: 161 / 161
- turns: 760
- mismatches: 0
- sandbox official writes: 0
- sandbox notifications: 0
- travel candidates: 53
- case-progress candidates: 47

Final daily execution smoke:

```powershell
python scripts\replay_agent2_daily_execution.py evals\agent2\dialogues --output-dir outputs\agent2_smoke_daily_execution_20260704_final --gate-mode protective_gate --require-gray-ready
```

Result:

- dialogues: 161
- turns: 760
- failed: 0
- mismatches: 0
- risk turns: 0
- gray ready: true

### Capability And Golden Smoke

Agent Core capability smoke:

```powershell
python -m pytest tests\test_agent_core_processor.py tests\test_agent_core_monthly_capability.py tests\test_agent_core_travel_capability.py tests\test_agent_core_case_progress_capability.py -q
# 26 passed
```

Golden harness:

```powershell
python scripts\eval_agent2_harness.py --cases evals\agent2\golden --output-dir outputs\agent2_golden_harness_real_smoke_20260704 --gate-mode protective_gate --fail-on-severity low
# 29 total, 29 passed, 0 failed, 0 unexpected failures
```

### Not Yet Claimed

- This was offline replay and smoke only.
- No online DingTalk smoke was run.
- No production write path was enabled.
- No full local pytest suite pass is claimed because existing collection and
  dependency issues outside this slice remain unresolved.
