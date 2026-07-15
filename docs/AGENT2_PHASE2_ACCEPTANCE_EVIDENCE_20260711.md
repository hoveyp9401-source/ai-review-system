# Agent2 Phase 2 acceptance evidence — 2026-07-11

Current verdict: `PARTIAL_IMPLEMENTATION`

This is an evidence ledger, not a cutover approval. The only approval verdict is `AGENT2_REPLACES_AGENT1_IN_TEST_TENANT`, and the current external/runtime state does not satisfy it.

## Requirement evidence

| Area | Current evidence | Status |
|---|---|---|
| Agent2-only primary chain | Stream/Webhook `agent2_primary` builds Agent2 context directly, invokes Cognitive Core v3, deterministic planner and typed executors; it does not invoke Shadow/Agent1 semantics or replies and has no automatic legacy fallback | Implemented locally |
| Conversation state | Command-bearing turns retain the loaded base state until every planned Daily and Business action has an authorized/executed or duplicate receipt; planner blocks, missing results, failures and partial execution preserve the base state. Pending is consumed in the same single optimistic state save after success | Implemented and tested locally; production concurrency/UoW not exercised against PostgreSQL |
| Daily receipts/replay | Executed, blocked and idempotent duplicate Daily outcomes carry deterministic receipt IDs and persist tenant-scoped receipt rows with exact command identity, snapshots, write flag and audit. Database receipt uniqueness is authoritative; successful replay does not upsert the report, blocked replay stays blocked, and receipt-to-plan command IDs must match before state advances | Implemented/tested locally; receipt migration not run on real PostgreSQL |
| Daily parity | Matrix covers append/multi-item/query/history/exact edit/delete/merge/copy/submit/status/pending/clear/reopen/section projection/previous-plan completion/mixed chat/case/travel | Implemented locally; real parity replay and test-tenant smoke still missing |
| Party knowledge | PostgreSQL models and migration include entities, aliases, identifiers, case roles, relations, typed case clues, sources, merge candidates and conflicts | Implemented locally; not migrated into a real DB |
| Party import | Dry-run fixture: 3 cases, 4 canonical parties, 5 aliases, 2 identifiers, 5 case roles, 1 relation, 3 typed clues, 5 sources, 1 merge candidate, 1 conflict | Dry-run only; zero persisted test-tenant rows proven |
| Party query | Exact/confirmed identity only; fuzzy candidates clarify. Results are tenant/case filtered and include visible relations plus person/court/payment/asset/document clues | Implemented/tested locally |
| Travel collaboration | Same tenant/company/department/team/city, overlapping dates, confidence threshold, grouped multi-person candidates, idempotent outbox, retries/dead-letter, cancel/change invalidation, two-sided acceptance | Implemented/tested locally; no real DingTalk delivery proven |
| Case progress | Create/update/delete/query/link, unique target policy, versioning, soft delete, source, actor, receipt and audit | Implemented/tested locally; no real PostgreSQL CRUD smoke |
| Cross-domain | Daily, CaseProgress and Travel remain separate commands/results for one source message | Local E2E test only |
| Legal Ops evidence UI | `/legal-ops/` and assets return HTTP 200; UI includes parties, case drill-down into the internal-progress lifecycle, relations/clues, travel, notifications, Daily/Business receipts, audits and failures | Shell/UI available; Phase 2 API returns 404 because feature and tenant allowlist are disabled |
| Routing/cutover | Runtime settings show Phase 2 disabled, Cognitive Core v3 disabled, travel worker disabled and zero allowed tenants | Not cut over |
| Rollback | Explicit route-control model, audit and runbook exist; default rollback flag is off | Implemented locally; no live rollback exercise |

## Verification commands and results

- Agent2 + Legal Ops focused suite: `819 passed, 1 warning`.
- Python compile verification: `python -m compileall -q app scripts tests` passed.
- Full repository suite: `1385 passed, 142 failed, 1 warning`.
  - The 142 failures remain in pre-existing Agent Core/Report Agent baselines outside the Phase 2 files (for example temporal-prefix normalization and legacy report-state behavior).
  - The Agent2 + Legal Ops focused suite is green, but the repository as a whole is not green.
- Legal Ops shell smoke:
  - `/health`: 200
  - `/legal-ops/`: 200
  - `/legal-ops/assets/app.js`: 200 and contains Agent2/clue UI
  - `/legal-ops/api/phase2`: 404 (expected safe default while disabled)
- Legal Ops sandbox verifier: valid, zero violations.
- Party fixture dry-run counts: cases 3; parties 4; aliases 5; identifiers 2; case roles 5; relations 1; clues 3; source references 5; merge candidates 1; conflicts 1.

## Missing acceptance proof

The following requirements have no authoritative evidence and therefore block replacement approval:

1. No local PostgreSQL server, container runtime or WSL distribution is available, so the migration, real import and database CRUD smoke have not run.
2. No identified test tenant is enabled or allowlisted; Phase 2 and Cognitive Core v3 are disabled in current runtime settings.
3. No real DingTalk identity binding, two-user travel match, outbound collaboration message, transport receipt or two-sided acceptance has been observed.
4. No read-only real-message Shadow comparison, Canary run, full test-tenant cutover or explicit rollback exercise has been performed.
5. No persisted test-tenant counts can be reported for parties, relations, clues, progress rows or successful travel matches.
6. The complete repository test suite has 142 unrelated baseline failures and is not globally green.
7. Live conversation state is not yet tenant-keyed and the primary path has no conversation fencing/lease. Business execution currently commits in a separate session before the conversation-state CAS, so the ADR-0007 same-UoW/concurrency gate is not proven and must be closed or explicitly redesigned before live cutover.

## Decision

Do not claim that Agent2 replaces Agent1 yet. The correct current verdict is `PARTIAL_IMPLEMENTATION`. The next admissible verdict can be `READY_FOR_TEST_TENANT_CUTOVER` only after real PostgreSQL migration/import/CRUD smoke, real transport smoke and a green test-tenant Canary. `AGENT2_REPLACES_AGENT1_IN_TEST_TENANT` additionally requires the test tenant to default to Agent2, Agent1 primary routing to be off, and explicit rollback to be exercised without automatic fallback or double writes.
