# Agent2 Release Baseline P1 Regression Adjudication Evidence

- Evidence date: 2026-07-15 (Asia/Shanghai)
- Branch: `codex/agent2-release-baseline-20260715`
- Scope: the 142 failures frozen at the P0 release baseline
- Release effect: no Canary expansion; no proactive Follow-up send; no automatic report projection

## Reproducible inputs

| Artifact | SHA-256 | Meaning |
|---|---|---|
| `report-state-before-fix-20260715.xml` | `C126637E5CCE8D4A289799937BA3CACF6F5000E4BBC51890877F3723C692F502` | Frozen 136 report-state failures |
| `report-state-current-20260715.xml` | `9301158F542CE310A96DA52976C4A8EC0CE76C45ED2ED6213EE74D3CAC9D0B0A` | Current focused report suite: 59 failed, 152 passed |
| `regression-adjudication-20260715.json` | `205D11B0DF12FE05147ADE446593E4A14776C82964FDC660C8EF563070DB30B1` | Per-test adjudication for all 142 frozen failures |
| `full-suite-20260715.xml` | `05C8F67FCFF557700B0A43562F8F23536355B2A867651B690646451DAC0C80C5` | Full repository: 59 failed, 2406 passed, 1 skipped |

Reproduce the report run:

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_report_agent_state_protocol.py `
  --junitxml=artifacts/agent2-release-baseline/p1/report-state-current-20260715.xml -q
```

Reproduce the adjudication:

```powershell
.\venv\Scripts\python.exe scripts/adjudicate_agent2_release_regressions.py `
  --baseline-report-junit artifacts/agent2-release-baseline/p1/report-state-before-fix-20260715.xml `
  --current-report-junit artifacts/agent2-release-baseline/p1/report-state-current-20260715.xml `
  --output artifacts/agent2-release-baseline/p1/regression-adjudication-20260715.json
```

The adjudicator checked that the current 59 failures are a strict subset of the
frozen 136 report failures. New current failures: **0**.

The full-suite failure identity set exactly equals the 59-test focused report
failure set (`full_minus_report=[]`, `report_minus_full=[]`). No Agent2 Core,
travel, case, follow-up, projection, routing, API or Legal Ops test failed in
the full run.

## Adjudication result

| Classification | Count | Release treatment |
|---|---:|---|
| `fixed_valid_regression` | 83 | Current gate passed |
| `unresolved_valid_regression` | 35 | Blocker for the affected legacy flow |
| `superseded_behavior_contract` | 15 | Keep current explicit-date/direct-rule contract; replace obsolete assertion separately |
| `superseded_outcome_semantics` | 4 | Replace `report_saved` assertion with Outcome/receipt evidence |
| `test_fixture_drift` | 3 | Repair the incomplete historical clock fixture before using it as runtime evidence |
| `accepted_security_strengthening` | 2 | Keep the safer non-enumerating and fact-source behavior |
| **Total** | **142** | Every frozen failure has one machine-readable record |

## Valid fixes demonstrated in this slice

- Weekday messages before 09:00 now default to the actual local date; Saturday
  before 09:00 still defaults to Friday.
- A current fact that mentions yesterday is not routed into yesterday's report.
- A turn with current work and a future plan is not collapsed into one plan item.
- Direct Agent Core facts retain `今天`/`明天` in operation and replay snapshots.
- Missing report slots do not capture `谢谢` and similar social acknowledgements.
- Ambiguous “不要拆这么碎” is treated as a merge clarification, not a deletion.
- A previous-plan completion with no previous-plan reference performs zero
  business writes and does not persist an unverified content candidate.
- The report prompt action schema now includes `unsubmit_report`; the pending
  executor no longer crashes on missing index normalization.

## Remaining blocker families

The 35 valid failures are concentrated in:

- current and historical edit cursor continuity;
- focused-field and target-date drift rejection;
- history-query-followed-by-edit continuity;
- previous-plan range/reference rollover;
- delete, clear, restore and short-confirmation pending lifecycles;
- correction against stable last-modified item references;
- full-template reference handling and exact item-boundary preservation.

They remain visible in the machine-readable artifact. No test was deleted,
skipped or xfailed. These blockers prevent a claim that the legacy report
protocol is fully green and prevent Agent2 from being declared a full Agent1
replacement.

## P1 decision

`P1_ADJUDICATION_COMPLETE_WITH_BLOCKERS`

The frozen 142 failures are now individually decided and no new focused-suite
failure was introduced. The 35 valid legacy-flow regressions remain explicit
release blockers for those flows. This evidence alone does not authorize
production expansion.
