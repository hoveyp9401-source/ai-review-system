# Agent2 Daily Release Gate Evidence Index

## Final evidence for `93ff1d7a`

- `AGENT2_DAILY_RELEASE_GATE_REPORT.md` — human-readable final report.
- `TEST_SUMMARY_93ff1d7a.json` — commands, exit codes, counts, failure-set
  comparison, PostgreSQL result, and round trip.
- `agent2_full_93ff1d7a.xml` — final Agent2 JUnit.
- `full_repo_93ff1d7a.xml` — final repository JUnit.
- `agent2_daily_context_postgres_gate_93ff1d7a.json` — exact final archive's
  real PostgreSQL result.
- `SPEC_REVIEW_93ff1d7a.md` — independent read-only Spec review.
- `STANDARDS_REVIEW_93ff1d7a.md` — independent read-only Standards review.
- `REPORT_ISSUE_LEDGER_ADDENDUM_93ff1d7a.md` — findings without touching the
  ignored user-owned root ledger.
- `CANARY_PREPARATION_MANIFEST_93ff1d7a.json` — safe counts and hashes only.
- `PATCH_MANIFEST_93ff1d7a.json` — commit/archive/patch identities.
- `agent2_daily_release_gate_forward.patch` — `c5773167` to `93ff1d7a`.
- `agent2_daily_release_gate_rollback.patch` — exact inverse.

The private Canary plan is not included. It remains mode `0600` in the isolated
server verification directory and is referenced only by hash.

## Chronological but superseded PostgreSQL artifacts

- `round1_no_go.json` — verifier field-level evidence defect.
- `round2_no_go.json` — diagnostic evidence weakness.
- `round3_candidate_duplicate_failure.json` — real candidate duplicate-receipt
  crash that led to the P0 fix.
- `agent2_daily_context_postgres_gate.json` — pre-commit passing run.
- `agent2_daily_context_postgres_gate_final.json` — exact e902 passing run,
  superseded after Standards found the `public` search-path P1.

## Earlier regression artifacts retained for comparison

- `agent2_full.xml`
- `full_repo.xml`
- `full_repo_failure_nodes.txt`

These are not the final HEAD results; they provide the frozen failure-node set
used to prove candidate-only failures equal zero.

## Authorized two-user Canary deployment

- `AGENT2_DAILY_TWO_USER_CANARY_DEPLOYMENT_REPORT.md` — deployment decision,
  route/rollback state, smoke results, open gaps, and rollback checklist.
- `REPORT_ISSUE_LEDGER_DEPLOYMENT_ADDENDUM_93ff1d7a.md` — deployment findings
  without modifying the user-owned root ledger.
- `deployment_20260722_93ff1d7a/` — safe server-side deployment, hash, health,
  smoke, parity, cleanup, and two-user read-only probe artifacts.

The deployment artifact directory excludes `private_canary_plan.json` and all
stable user IDs. Real Canary evidence contains only irreversible hashes,
counts, categories, and booleans.
