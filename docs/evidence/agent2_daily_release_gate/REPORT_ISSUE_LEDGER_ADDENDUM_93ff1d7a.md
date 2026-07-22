# Daily Report Issue Ledger Addendum

The clean candidate intentionally does not write the ignored, user-owned root
`REPORT_ISSUE_LEDGER.md`. These release-gate findings are retained here so the
original dirty worktree remains untouched.

| ID | Severity | Finding | Resolution | Evidence |
|---|---|---|---|---|
| DAILY-GATE-20260722-01 | P0 | A real-shaped duplicate receipt caused replay to access nonexistent `snapshot.report_date`, raising before idempotent return. | Fixed in `e902bb6b`: compare receipt date with the date derived from the typed command and execution context. | Strengthened executor regression; real PostgreSQL duplicate/concurrency checks PASS. |
| DAILY-GATE-20260722-02 | P1 verification | Gate runtime search path contained `public`, allowing an omitted clone to fall through to production tables. | Fixed in `93ff1d7a`: runtime search path is exactly the unique gate schema plus `pg_catalog`. | Dedicated unit test; independent Spec/Standards PASS; final PostgreSQL gate PASS. |
| DAILY-GATE-20260722-03 | P2 | Gate zero-I/O counters are declarative rather than instrumented. | Open and disclosed; the script imports no model/message client and executes the typed executor only against the isolated DB session. | Spec and Standards reviews. |
| DAILY-GATE-20260722-04 | Rollout blocker | Production checkout has 280 existing status entries and Agent1 rollback is disabled. | Not changed. Requires human-owned clean deployment procedure and rollback enablement before activation. | Read-only server identity and safe Canary manifest. |
| DAILY-GATE-20260722-05 | Product scope | Pure Case/future-domain content outside Daily context is not yet universally projected into Daily. | Explicitly outside this release gate; do not claim broad cross-domain absorption. | Frozen release candidate report and current scope review. |
