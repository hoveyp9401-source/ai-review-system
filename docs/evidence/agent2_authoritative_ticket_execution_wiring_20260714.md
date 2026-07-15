# Agent2 Authoritative Ticket Execution Wiring Evidence — 2026-07-14

## Scope

This slice closes the execution-side authority gap for production report and
business mutations. It does not change Domain Admission policy or issue new
Tickets.

## Transaction contract

For an enforced mutation, one caller-owned PostgreSQL transaction now contains:

1. Admission trace/decision/Ticket issuance through `SqlAdmissionArtifactSink`;
2. authoritative Ticket row lock and revalidation;
3. the domain mutation;
4. the exact domain receipt and audit;
5. Ticket consumption linked to that successful receipt;
6. the final outer commit.

Webhook and Stream no longer open a second `AsyncSession` for
`Phase2BusinessComposer`. The composer, repositories, `SqlBusinessExecutor`,
report executors, and Admission artifact sink all receive the current turn
session. Therefore an executor can see the uncommitted Ticket issued earlier in
the same transaction without weakening the authority check or adding an unsafe
intermediate commit.

Webhook, Stream, and Manual also share `production_agent2_turn_runtime()`, which
installs both `SqlAdmissionArtifactSink` and the SQL Information Pending
continuation adapter. Each adapter still supplies its current session through
`VerifiedTurnRequest`, so issuance, continuation handling, and execution use the
same transactional visibility boundary.

## Typed daily guarantees

- Default production store: `SqlAdmissionTicketStore(session)`.
- Every enforced daily mutation performs authoritative `acquire` before any
  report write.
- Ticket scope, operation, object ID/version, state version, TTL, status, exact
  canonical Ticket payload, and claim hashes are revalidated by the SQL store
  and local closed command contract.
- Multiple daily mutations in one turn use the working report versions in
  order; each command owns and consumes one Ticket.
- Daily report effect, command receipts, and Ticket consumption share one commit
  boundary.
- Receipts are flushed before Ticket consumption so the store can verify the
  receipt row and exact `status`/`actual_write` values.
- A missing, expired, inactive, already-consumed, or authority-mismatched Ticket
  produces zero report writes and no consumption.
- Successful duplicate replay is detected before Ticket reacquisition, returns
  the stable original receipt identifier, and never consumes a Ticket again.
- Receipts are reloaded after acquiring the report advisory lock to close the
  race where another worker completes while the current worker waits.

## Other mutation executors

- `execute_periodic_report_commands` defaults to
  `SqlAdmissionTicketStore(session)` and already consumes only after its
  persisted periodic receipt.
- `SqlBusinessExecutor` defaults to `SqlAdmissionTicketStore(session)` and keeps
  domain effect, receipt, audit, and Ticket consumption in nested savepoint plus
  the caller's outer transaction.
- Manual typed-daily execution inherits the same authoritative SQL default.

## Remaining business mutation revalidation

The authoritative SQL Ticket store now re-locks and revalidates live objects
for every supported production mutation before the executor may write:

- case-progress update, delete, and link: tenant/case visibility, actor
  ownership or explicit admin role, non-deleted state, and optimistic version;
- travel collaboration response: tenant/company/department/team scope,
  participant identity, writable lifecycle state, expiry, optimistic version,
  and absence of an earlier response by the same participant;
- case follow-up policy update and immediate trigger: tenant/case visibility,
  current assignee or explicit admin role, policy version (including a missing
  policy at version `0`), and no already-active task for an immediate trigger.

The command claims, authorized changed-field set, and Ticket hashes are checked
as a closed contract. Optional update/link/policy fields must form a non-empty
canonical subset; unknown fields, reordered fields, claim drift, and version or
permission drift fail before the domain write. Case follow-up Ticket contracts
use the canonical `domain="case"`.

## Reproducible tests

Focused command:

```powershell
.\venv\Scripts\python.exe -m pytest -q `
  tests/test_agent2_authoritative_ticket_wiring.py `
  tests/test_agent2_typed_daily_executor_v3.py `
  tests/test_agent2_report_sql_admission_store.py `
  tests/test_agent2_sql_admission_ticket_store.py `
  tests/test_agent2_turn_runtime.py `
  tests/test_agent2_information_pending_runtime_wiring.py `
  tests/test_agent2_manual_context_wiring.py `
  tests/test_agent2_stream_context_wiring.py `
  tests/test_agent2_daily_entrypoint_consistency.py `
  tests/test_agent2_business_sql_executor.py `
  tests/test_agent2_business_entrypoint.py `
  tests/test_agent2_sql_admission_mutation_revalidation.py
```

Observed result: `127 passed`.

Coverage includes:

- sequential two-command daily versions and one-consumption-per-Ticket;
- missing/expired/inactive/authority-mismatch zero-write cases;
- expired/spent Ticket duplicate replay without reacquisition;
- receipt flush ordering;
- same-session Webhook/Stream structural guard;
- default SQL store injection for daily, periodic, and business executors.

The complete currently discoverable Agent2 regression command produced
`1251 passed, 1 skipped, 0 failed` in 24.29 seconds. The skip is pre-existing;
this slice did not add or change any skip/xfail marker.

No server deployment or real-user claim is made by this evidence file.
