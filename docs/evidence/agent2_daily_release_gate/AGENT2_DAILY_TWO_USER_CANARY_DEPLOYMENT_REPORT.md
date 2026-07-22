# Agent2 Daily Two-User Canary Deployment Report

## Decision

**DEPLOYED_TO_TWO_USER_CANARY — WATCHING**

Candidate `93ff1d7a0f07b2d244071e37f1d5efb359acef58` is deployed to the
existing two-user Agent2 Canary. Agent1 rollback is enabled. The Canary set was
not expanded, no real message was sent, and no push or merge was performed.

The deployment has no observed candidate-only P0. It is intentionally not
approved for a broader audience yet: the online issue-ledger run retains one
stable legacy/model-drift gap, the production Product Gate has three pre-existing
cutoff-date failures, and universal Case/other-domain-to-Daily projection is not
part of this candidate.

## 1. Identity and scope

- Candidate: `93ff1d7a0f07b2d244071e37f1d5efb359acef58`
- Candidate branch: `codex/agent2-daily-release-gate`
- Production branch / Git HEAD: `main` /
  `96cecec81c4ac9fe4e0500c08a52e661d094147d`
- Production status entries before and after: 280 / 280
- Candidate archive SHA-256:
  `6d619622bc0cb51f8f8137746e61cc96d333af45184a80af60372b57ce1e4f5b`
- Deployment time: 2026-07-22, Asia/Shanghai
- Deployed release files: 12
- Production service configuration and `.env`: unchanged
- Production database schema: unchanged
- Dependency and lock files: unchanged

The production checkout already contained 280 unrelated status entries. A
whole-tree replacement would have overwritten unreviewed production hotfixes,
so deployment was limited to the 12 files reviewed as the Daily release scope:

1. `app/agent2/cognitive_contract_v3.py`
2. `app/agent2/command_planner_v3.py`
3. `app/agent2/report_document_contract.py`
4. `app/agent2/runtime/context.py`
5. `app/agent2/runtime/domains.py`
6. `app/agent2/runtime/harness.py`
7. `app/agent2/semantic_interpreter_v3.py`
8. `app/agent2/typed_daily_commands.py`
9. `app/agent2/typed_daily_executor.py`
10. `app/api/reports.py`
11. `app/api/webhook.py`
12. `app/stream_runner.py`

Every prior production file was backed up before replacement. The final
production hashes for all 12 files exactly match the candidate manifest.

## 2. Route and rollback controls

| Control | Final state |
|---|---|
| Route mode | `agent2_canary` |
| Canary users | 2 |
| Canary scope matches approved snapshot | yes |
| Agent1 rollback | enabled |
| Route-control version | 4 |
| Wildcard or audience expansion | none |

The private plan containing stable user IDs remains mode `0600` on the server
and is excluded from this evidence package. The safe final verification records
only the tenant hash, count, mode, rollback state, and scope equality.

## 3. Deployment chronology and recovery proof

1. A hybrid preflight assembled current production plus only the 12 candidate
   files. Imports passed and the targeted suite reported 55 passed.
2. Agent1 rollback was enabled through the existing audited route-control
   repository before candidate activation. The two-user set remained unchanged.
3. The first service-control attempt stopped before copying any file because
   non-interactive `systemctl` authorization was unavailable.
4. The first owner-signal attempt encountered root-owned Python cache content.
   The automatic rollback restored all 12 prior files byte-for-byte and resumed
   the old processes. That failed attempt is retained in the evidence chronology.
5. The successful attempt froze the owner processes, replaced only the 12
   explicit files, and used the units' existing `Restart=always` behavior.
6. API, stream, and scheduler restarted successfully. Health returned OK.

No failed attempt left a partial candidate active.

## 4. Post-deployment verification

Final state at 2026-07-22 18:10:00 +08:00:

| Check | Result |
|---|---|
| API service | active |
| Stream service | active |
| Scheduler service | active |
| Health endpoint | OK |
| All 12 candidate hashes | exact match |
| Agent1 rollback | enabled |
| Canary audience | exact approved two-user set |
| Temporary baseline port 18001 | closed |
| Pytest/smoke/baseline processes | none |
| Synthetic smoke users | 0 |
| Synthetic smoke teams | 0 |
| Synthetic smoke webhook rows | 0 |

One synthetic issue-ledger team row remained after the first cleanup. It was
deleted only after verifying the exact team code, one matching row, and zero
referencing users. The final residue counters are all zero.

## 5. Tests and online smoke

| Scope | Result | Interpretation |
|---|---:|---|
| Hybrid targeted preflight | 55 passed | Candidate files import and run over current production. |
| Progress outbox gate | 12 passed | Progress outbox behavior remained green. |
| Online system smoke | 7 / 7 passed | Health, idempotency, concurrency, isolation, copy/numbered preview, plan range, and short display passed. |
| Online issue-ledger smoke | 102 / 104 passed | Two failures were investigated instead of hidden. |
| Existing two-user read-only Canary probes | 2 / 2 passed | Both traversed Agent2 read-only routing with no report mutation. |
| Product quick gate | 137 passed / 3 failed | Current production gate is not fully green; failures are the unchanged legacy previous-report cutoff cases listed below. |

The three Product Gate failures are:

- `test_previous_report_backfill_before_nine_uses_previous_date`
- `test_weekday_before_nine_bare_report_defaults_to_previous_reporting_date`
- `test_monday_before_nine_bare_report_defaults_to_friday`

The implementation under those failures was not part of the 12-file release
scope and was unchanged by deployment. They remain a visible legacy gate gap;
they are not presented as fixed.

The initial 104-case online run failed:

- `DR-012-01-merge-range`
- `DR-017-03-last-not-done`

Follow-up results:

- DR-012: candidate 0/3 and backed-up baseline 0/3. This is reproducible
  current model/legacy behavior with parity across candidate and baseline, not
  a candidate-only regression. It remains open.
- DR-017: candidate repeat 3/3 and baseline 1/1. The first failure was transient
  model variability and was not reproduced.

The temporary baseline API was stopped and its port is closed.

## 6. Existing-Canary read-only probe

Each approved Canary user received one loopback/manual read-only Daily query
using a temporary isolated conversation ID. No DingTalk or other outbound
message was sent.

For both users:

- HTTP status: 200
- route result: Agent2 read-only
- report count and report content hash: unchanged
- mutation receipt delta: 0
- query receipt delta: 1, as expected for read-only auditability
- route audit delta: 1
- temporary conversation-state row: removed

The artifact contains only hashes, counts, response categories, and booleans;
it contains no user name, stable user ID, or business text.

## 7. Safety and privacy

- Real business writes caused by deployment smoke: 0
- Real outbound messages: 0
- Real Daily report mutations in Canary probe: 0
- Canary audience expansion: 0
- Database migrations: 0
- Production configuration changes: 0
- Private user identifiers in packaged evidence: 0
- Synthetic smoke residue: 0

The online smoke uses synthetic fixtures. The real-user check is read-only and
stores only irreversible hashes in the evidence artifact.

## 8. Open findings

### P1 operational watch: DR-012 merge range

The model currently asks for clarification or fails to collapse the requested
range. Baseline and candidate both reproduced the issue in all three repeats.
It is not a reason to attribute regression to `93ff1d7a`, but it remains a real
user-facing gap to monitor and fix in a separate bounded change.

### P2 legacy Product Gate failures

Three before-09:00/previous-report-date tests remain red in current production.
They are outside this candidate and must remain visible until independently
fixed and deployed.

### Product-scope limitation

The candidate improves the reviewed Daily context and typed-command paths. It
does not implement a universal policy that automatically projects all pure Case,
Travel, or future-domain content into Daily. Broader cross-domain absorption
must not be advertised as complete.

## 9. Rollback

The immediate logical rollback is already available through Agent1 fallback.
If a candidate-only P0 appears, stop the two-user Agent2 route or use the
audited route-control snapshot, then restore the 12 backed-up files from:

`/home/ai_review_tunnel/verification/agent2-daily-release-gate-release_20260722_93ff1d7a/deployment_20260722_93ff1d7a/rollback_files`

After restoration, restart the three existing services and verify the rollback
manifest, health, route scope, and absence of synthetic residue. Do not reset or
clean the production checkout because its unrelated status entries are
user-owned.

## 10. Human watch checklist

For the existing two users, watch:

1. Daily content being diverted to internal Q&A after an explicit Daily goal.
2. Section correction such as moving prior content to tomorrow's plan.
3. False-success replies when no database change occurred.
4. Partial updates clearing untouched sections.
5. Duplicate delivery, repeated confirmation, and stale Pending revival.
6. DR-012-style range merge instructions.
7. Case or Travel content being incorrectly advertised as universally absorbed
   into Daily.

Any candidate-only wrong write, section loss, false success, or privacy leak is
a P0 and should trigger immediate Agent1 rollback. Audience expansion requires a
separate human decision and fresh evidence.
