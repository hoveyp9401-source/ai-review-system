# Agent2 Daily Context Release Gate Report

## Decision

- Candidate code gate: **PASS**
- Real PostgreSQL gate: **PASS**
- Independent Spec review: **PASS** (P0 0 / P1 0)
- Independent Standards review: **PASS** (P0 0 / P1 0)
- Rollout decision: **CONDITIONAL**

The Daily candidate is now proven against real PostgreSQL and has zero
candidate-only test failures. It is not deployed and no feature flag, route,
Canary membership, service, business row, or production code was changed.

Rollout remains conditional because the production checkout has 280 existing
status entries, Agent1 rollback is currently disabled, and the two named test
users are already the complete two-user route-control Canary. Expanding to
additional people requires the human reviewer to identify stable user IDs; no
people were invented. The broader pure-Case/future-domain-to-Daily projection
policy also remains outside this candidate and must not be advertised as done.

## 1. Candidate identity and isolation

- Branch: `codex/agent2-daily-release-gate`
- Fixed point: `c577316751ba312968fe85dc01cd4b688fd48369`
- Candidate commits:
  - `e902bb6bb4a229d2269420f15c8f82cfdb0709cf`
  - `93ff1d7a0f07b2d244071e37f1d5efb359acef58`
- Final candidate SHA: `93ff1d7a0f07b2d244071e37f1d5efb359acef58`
- Candidate archive SHA-256:
  `6d619622bc0cb51f8f8137746e61cc96d333af45184a80af60372b57ce1e4f5b`
- Exact server verification directory:
  `/home/ai_review_tunnel/verification/agent2-daily-release-gate-release_20260722_93ff1d7a`
- Original dirty worktree was not modified, reset, staged, or committed.

The final archive was generated from committed `HEAD`, copied to the isolated
server directory, and verified server-side against the same archive SHA before
execution.

## 2. Change scope

Compared with the frozen Daily release candidate, this gate changes four files:

1. `app/agent2/typed_daily_executor.py`
   - Fixes duplicate-receipt replay so the receipt date is compared to the
     report date derived from the typed command and execution context.
   - Removes the invalid dependency on `snapshot.report_date`.
2. `scripts/verify_agent2_daily_context_postgres.py`
   - Adds a fail-closed, real PostgreSQL gate using a unique temporary schema,
     synthetic rows, transaction rollback checks, and automatic cleanup.
   - Runtime SQL search path is exactly `<temporary_schema>,pg_catalog`.
3. `tests/test_agent2_daily_context_postgres_gate.py`
   - Covers identifier validation, isolation plan, fail-closed evaluation,
     artifact privacy, cleanup contract, and prohibition of `public` fallback.
4. `tests/test_agent2_typed_daily_executor_v3.py`
   - Strengthens duplicate-receipt evidence with a real-shaped receipt that
     contains `report_date`.

No Prompt, semantic rule, Admission policy, Ticket/Typed Command contract,
Receipt/Audit contract, permission rule, database schema/migration, lockfile,
dependency, external API, message path, production default, or Canary route was
changed.

## 3. Findings and fixes

### Closed P0: duplicate receipt replay crash

The first real PostgreSQL run exposed a candidate defect: a duplicate receipt
path accessed `snapshot.report_date`, but the snapshot type has no such field.
That could break safe idempotent replay before returning the prior result.

The fix derives the expected date from the typed command and execution context,
then compares it with the authoritative receipt. The strengthened regression
test and real PostgreSQL duplicate/concurrent delivery scenarios now pass.

### Closed P1 verification defect: `public` fallback

The first independent Standards review found that the gate's runtime
`search_path` was `<temporary_schema>,public`. If a clone were accidentally
omitted, application SQL could read or write the production table. The gate was
therefore not accepted even though its preliminary run passed.

Commit `93ff1d7a` changes the runtime path to
`<temporary_schema>,pg_catalog`. A dedicated test prohibits `public`, and the
exact final archive passed the real PostgreSQL gate with this strict path.

### Open P2 evidence limitations

- Model, external API, and message counters are declared zero by the bounded
  gate rather than collected from instrumented clients. The verifier imports no
  model/message client and invokes only the typed Daily executor.
- Public-schema comparison checks the gate's unique synthetic markers rather
  than performing a database-wide change audit.
- Reply verification compares the committed structured snapshot, not the final
  prose renderer.
- The gate validates candidate SHA syntax but does not derive Git identity.
  Exact archive SHA verification and a candidate-bound private Canary plan are
  retained as compensating evidence.

None of these P2 items permits deployment by itself; they are disclosed for the
human release review.

## 4. Real PostgreSQL gate

The final run used:

- Run ID: `release_20260722_93ff1d7a`
- Candidate: `93ff1d7a0f07b2d244071e37f1d5efb359acef58`
- Transport: SSH into the isolated verification directory
- Application search path: unique gate schema plus `pg_catalog`
- Data: synthetic users, report, events, and receipts only
- Model calls: 0
- External API calls: 0
- Message sends: 0

Result: **PASS**, with no failed checks.

| Check | Result |
|---|---:|
| Stable item-ID move is atomic | PASS |
| Partial update preserves other sections | PASS |
| Three commands advance versions | PASS |
| Duplicate delivery is idempotent | PASS |
| Concurrent duplicate is serialized | PASS |
| No-op preserves row and version | PASS |
| Reply matches committed structured snapshot | PASS |
| Transaction rollback is atomic | PASS |
| Public synthetic markers unchanged | PASS |
| Temporary schema cleanup confirmed | PASS |
| Writes outside isolated schema | 0 |

Metrics: final report version 7, receipt count 7, concurrent path 40.631 ms.

Final artifact:

- `agent2_daily_context_postgres_gate_93ff1d7a.json`
- Internal canonical report SHA-256:
  `f7988dcb316d131a3cdfbf6299a6b3889679aee813348e57d01bece5a03edaa4`
- Stored file SHA-256:
  `5cbb29973cc4d394983e5396c146873e71a83fe7f2ac961e477487e3aac1e55a`

Preliminary e902 gate artifacts are retained for chronology but are explicitly
superseded because they predate the strict-search-path fix.

## 5. Test results

| Scope | Exit | Passed | Failed | Skipped |
|---|---:|---:|---:|---:|
| PostgreSQL gate unit tests | 0 | 7 | 0 | 0 |
| Daily release targeted suite | 0 | 62 | 0 | 0 |
| Agent2 full suite | 0 | 1684 | 0 | 2 |
| Full repository | 1 | 2433 | 65 | 2 |
| Python compile | 0 | - | - | - |
| `git diff --check` | 0 | - | - | - |
| Forward/rollback round trip | 0 | - | - | - |

The full-repository command exits 1 because of the known 65 baseline failures.
The failure node-ID set is exactly identical to the frozen candidate evidence:

- Candidate-only failures: 0
- Base-only failures: 0
- Failure sets identical: yes

No test was deleted, skipped, xfailed, weakened, or replaced with a snapshot
update. Machine-readable command results are in
`TEST_SUMMARY_93ff1d7a.json`; current JUnit files are
`agent2_full_93ff1d7a.xml` and `full_repo_93ff1d7a.xml`.

## 6. Forward and rollback proof

- Forward patch SHA-256:
  `8cc400357f27d779f6819874ca6bed0e7aaf863761de7660f4fcfa593bb14128`
- Rollback patch SHA-256:
  `4dba9da129239279f5f53892f518bf6a5eb50434435ebf87c7a0ef4c7e08b538`

A detached worktree at `c5773167` applied the forward patch with `--index` and
matched the final `93ff1d7a` tree. Applying the rollback patch with `--index`
returned the worktree to zero status entries. The temporary worktree was then
removed.

## 7. Independent reviews

### Spec

PASS. P0 0, P1 0. The reviewer confirmed strict isolation, required PostgreSQL
scenarios, bounded production change, and no deployment/config/Canary scope
creep. Three evidence-strength P2s are recorded above.

### Standards

PASS. P0 0, P1 0. The reviewer confirmed fail-closed identifiers and confirmation
token, no runtime public fallback, privacy-preserving output, repository-aligned
tests, and reversible scope. Three evidence-strength P2s are recorded above.

Both reviews were independent and read-only.

## 8. Server and rollout state

Read-only server check after verification:

- Production branch: `main`
- Production HEAD: `96cecec81c4ac9fe4e0500c08a52e661d094147d`
- Production status entries: 280
- API service: active
- Stream service: active
- Scheduler service: active
- Health: OK
- Verification processes left running: 0

Code defaults remain false for Agent2 Daily, Cognitive Core v3, Business Phase
2, and Semantic Admission. The existing production environment is separately
configured for its current controlled Agent2 routes; this task did not alter it.

The final private Canary preparation plan is mode `0600`, is bound to
`93ff1d7a`, and is deliberately excluded from the evidence ZIP because it
contains stable IDs. Its safe metadata is:

- Private plan SHA-256:
  `508617b8f4167f05ee9b2ef4bc452ec5213cc2cfe8535d5881155af063281ead`
- Current route mode: `agent2_canary`
- Current/proposed route-control Canary count: 2 / 2
- Both named target users are already in that Canary: 2 / 2
- Current/proposed legacy Daily allowlist count: 1 / 2
- Current/proposed Agent1 rollback: false / true
- Allowed tenant count: 1
- Wildcard: not used
- Plan applied: no

The route-control Canary already contains both named testers. The legacy Daily
environment allowlist is a separate compatibility/manual-path control and must
not be mistaken for route-control expansion.

## 9. Human approval checklist

Before any deployment or activation, a human must:

1. Choose a clean, auditable deployment method that does not overwrite or
   absorb the 280 unrelated production status entries.
2. Review and explicitly enable the prepared Agent1 rollback fence before
   activating the candidate.
3. Confirm whether the initial audience is only the existing two users or
   provide additional stable user IDs and verified tenant bindings.
4. Reconcile route-control and legacy/manual Daily allowlists without using
   names or a wildcard.
5. Deploy the exact candidate/archive and verify the deployed SHA before any
   flag or route change.
6. Run real online conversation/system smoke with isolated users and retain DB
   and reply evidence before adding more people.
7. Monitor false-success replies, section loss, duplicate replay, model timeout,
   wrong Case binding, and cross-domain contamination; use immediate Agent1
   rollback if a P0 appears.
8. Do not claim universal Case/other-domain-to-Daily projection; that remains a
   separately specified and evaluated product change.

## 10. Final statement

The candidate itself has passed the code, real PostgreSQL, regression,
round-trip, Spec, and Standards gates. The server rollout is **CONDITIONAL** on
clean deployment authority, enabled rollback, and an explicitly identified
review audience.

No deployment, push, merge, service restart, production configuration change,
Canary mutation, production business write, external API call, or message send
was performed.
