# Agent2 Phase 2 migration

Status: implementation in progress. This document is not a cutover approval.

## Target chain

`DingTalk identity -> tenant route control -> Agent2 context -> Cognitive Core -> deterministic compiler -> typed command -> domain executor -> receipt/audit -> reply`

For a tenant in `agent2_primary`, Stream and HTTP Webhook both call `resolve_agent2_entrypoint`. A primary-route failure is fail-closed. Neither entrypoint calls the Agent1 natural-language router as a recovery mechanism. Agent1 remains reachable only by changing the audited tenant route control back to `agent1`; `agent1_rollback_enabled` defaults to false.

## Safety gates

- `AGENT2_BUSINESS_PHASE2_ENABLED=false` by default.
- `AGENT2_BUSINESS_TENANT_IDS` must explicitly list Sandbox/test tenants.
- A channel identity must resolve to exactly one active `agent2_identity_bindings` row in the allowlist.
- A tenant route must be `agent2_canary` or `agent2_primary` before Agent2 owns the message.
- Travel notification dispatch has a second explicit worker switch and uses the same tenant allowlist.
- Formal tenants are not automatically enrolled or switched.

## Implemented path

- PostgreSQL schema and transactional migration for identity, cases, parties, travel, notifications, case progress, Daily/Business receipts, audits and route control. The main `scripts/create_agent2_business_phase2_tables.sql` includes the Daily receipt table; `scripts/create_agent2_daily_command_receipts.sql` is the standalone additive upgrade for an already-created Phase 2 schema.
- Typed Phase 2 commands for TravelIntent and CaseProgress actions.
- Deterministic travel location/time canonicalization and case-target resolution.
- SQL executor with idempotency reservation, nested rollback, optimistic version checks, soft delete, receipt and audit.
- Structured Party resolver: exact identifier/name/confirmed alias can resolve; `pg_trgm` only returns candidates.
- Organization-scoped multi-person travel matching, outbox deduplication, retry/dead-letter and direct DingTalk transport receipt.
- Independent cross-domain business results for one source message.
- Stream and Webhook test-tenant routing without automatic Agent1 fallback. `agent2_primary` builds Agent2 context directly and does not invoke the Shadow/Agent1 semantic or reply chain.
- Cognitive v3 daily parity paths for append, exact edit/delete/merge, current/history query, copy, submit, status, bound-confirmation clear, section clear, reopen, current-work projection, previous-plan completion, and daily-plus-chat reply composition.
- Receipt-driven conversation state: command-bearing turns defer their proposed state until all planned Daily and Business outcomes succeed; planner blocks, missing/partial outcomes and failures retain the loaded base state, while bound pending is consumed only in the successful optimistic save.
- Every typed Daily execution, including query, block and idempotent replay, emits a deterministic receipt ID and persists a tenant-scoped `agent2_daily_command_receipts` row. The receipt table's tenant/idempotency uniqueness is the durable replay authority; report JSON keys remain compatibility metadata only. A successful replay returns `duplicate`, may complete a previously interrupted state transition, and does not upsert the report row. A previously blocked receipt remains blocked and cannot later write under the same key.
- `/legal-ops/api/phase2` and the Agent2 evidence page.

## Still required before cutover

- Run the migration against an identified Sandbox/test database and record DB smoke evidence.
- Import/sync real test-tenant cases and parties and report actual counts/conflicts.
- Bind real test users to tenant/company/department/team/case scopes.
- Run the full Agent1-vs-Agent2 daily parity replay against the now-complete migration matrix.
- Run message-transport, cross-domain E2E, canary and real test-tenant smoke.
- Exercise explicit rollback and prove that no automatic fallback or double write occurs.
