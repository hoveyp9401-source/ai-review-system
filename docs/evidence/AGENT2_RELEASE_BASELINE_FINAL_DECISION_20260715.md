# Agent2 Release Baseline — Final decision

Date: 2026-07-15 (Asia/Shanghai)

## Final decision

`SHADOW_ONLY`

The current Agent2 inbound Canary may remain available only for Pang Hao and
Liu Cong in tenant `sandbox-agent2-phase2-20260711`. Canary scope must not be
expanded. Agent2 is not approved to replace Agent1.

The Legal Ops product surface separately reached
`READY_FOR_TWO_USER_UI_CANARY`, but it has not reached
`TWO_USER_UI_CANARY_VALIDATED`. UI readiness does not override the runtime,
report-protocol or real-user gates.

## Gate matrix

| Gate | Evidence | Result |
|---|---|---|
| Frozen baseline | local/server/database/config/runtime/test inventory recorded | PASS |
| Rollback | pre-deploy archives, hashes and supervised restart procedure | PASS |
| Runtime ownership | one API, one Stream and one Scheduler instance | PASS |
| Route isolation | one tenant, exactly two canary users, Agent1 rollback off | PASS |
| Business write switches | case progress and travel enabled only inside the canary authority | PASS |
| Anti-double-write | single runtime owner plus travel business-fact dedup and auditable duplicate receipt | PASS |
| Regression identity | 2412 passed, 59 failed, 2 skipped; failure identity delta 0 | PASS for no-new-regression |
| Report behavior contract | 35 adjudicated valid report-protocol blockers remain | FAIL |
| Two-user current-runtime closure | no post-final-deploy organic human closure for both users | FAIL |
| Legal Ops truth/UI | real-data projection, Chinese labels, permission smoke and capability truth deployed | READY_FOR_TWO_USER_UI_CANARY |
| Proactive follow-up send | switch is off; no two-user delivery/reply closure | OFF / NOT PASSED |
| Automatic report projection | switch is off; no two-user production closure | OFF / NOT PASSED |

## Current production-safe boundary

The server currently has:

- semantic admission enabled but not enforced;
- one semantic tenant allowlist entry and two user allowlist entries;
- case-progress write enabled;
- travel write enabled;
- follow-up policy/task generation enabled;
- follow-up DingTalk sending disabled;
- case-to-report automatic projection disabled.

This boundary is intentional. A model interpretation cannot be used as evidence
that a message was sent, delivered, accepted by a user or written to a report.

## What is usable now

1. Pang Hao and Liu Cong may continue the existing inbound Agent2 gray test.
2. Both case-owner credentials can view the 80-case explicit shared scope and
   add case progress where the permission contract allows it.
3. Reports remain owner-scoped in Legal Ops.
4. The Legal Ops page is deployed at
   `http://124.221.205.13:8000/legal-ops/` and is ready for controlled two-user
   UI observation.
5. Administrators may configure follow-up policies and create shadow follow-up
   tasks. The UI states that these tasks are not currently sent to DingTalk.

## What is not approved

- expanding beyond Pang Hao and Liu Cong;
- treating Agent2 as an Agent1 replacement;
- enabling semantic admission enforcement globally;
- enabling proactive follow-up message sending;
- enabling automatic report projection;
- claiming Liu Cong has completed a successful current-runtime case/travel/report closure;
- claiming both users have validated the UI through real browser sessions;
- declaring the report chain production-safe while the 35 valid protocol blockers remain.

## Evidence summary

| Phase | Main artifact | Decision/result |
|---|---|---|
| P0.1 | `AGENT2_RELEASE_BASELINE_P0_1_SNAPSHOT_20260715.md` | frozen evidence baseline |
| P0.2 | `AGENT2_RELEASE_BASELINE_P0_2_INVENTORY_20260715.md` | rollback inventory |
| P0-B/C | `AGENT2_RELEASE_BASELINE_P0_BC_POSTDEPLOY_20260715.md` | unique runtime and route controls |
| P1 | `AGENT2_RELEASE_BASELINE_P1_REGRESSION_ADJUDICATION_20260715.md` | 35 valid report blockers |
| P2 | `AGENT2_RELEASE_BASELINE_P2_POSTDEPLOY_20260715.md` | shared case scope deployed |
| P3 | `AGENT2_RELEASE_BASELINE_P3_TWO_USER_ACCEPTANCE_20260715.md` | SHADOW_ONLY |
| P4 | `AGENT2_RELEASE_BASELINE_P4_LEGAL_OPS_UI_20260715.md` | READY_FOR_TWO_USER_UI_CANARY |

The final full-suite artifact is
`artifacts/agent2-release-baseline/p4/full-suite-ui-truth-reviewed-20260715.xml`
with SHA-256
`c8f4dc2b1f884d398ecd08d7e1a5ee2830c15107fed1e71b4300065e0cb8e995`.

## Conditions for the next decision

Reconsider `READY_FOR_TWO_USER_CANARY` only after all of the following are true:

1. the 35 valid report-protocol blockers are fixed without adding regressions;
2. Pang Hao and Liu Cong each complete current-runtime organic positive and
   negative flows with database/receipt evidence;
3. both users complete a real Legal Ops browser session;
4. follow-up task wording and projection decisions pass human review in shadow;
5. the relevant kill switch is enabled only for a bounded trial and can be
   immediately rolled back;
6. observation produces zero wrong-case writes, cross-user writes, duplicate
   business writes, false send/delivery claims and report-context hijacks.

Until then, the release baseline is complete as an evidence-backed decision,
not as a production-replacement approval.

