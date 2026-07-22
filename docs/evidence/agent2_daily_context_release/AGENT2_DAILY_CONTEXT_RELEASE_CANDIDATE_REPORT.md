# Agent2 Daily Context Release Candidate Report

## Decision

`CONDITIONAL`

The clean candidate fixes the two historical Daily conversations and has zero
candidate-only test failures.  No P0 semantic or safety regression remains in
the exercised paths.  The mandatory real PostgreSQL Repository gate could not
be executed on this host because it has no PostgreSQL client/server/service,
container runtime, WSL distribution, or isolated test DSN.  This report does
not authorize deployment or Canary expansion.

## 1. Identity and isolation

- Original dirty worktree: `C:\Users\00020271\Documents\ai-review-system`
- Original branch: `codex/agent2-release-baseline-20260715`
- Original HEAD: `fd887eca22c8686df3ca26319344b713ca304fab`
- Initial tracked diff evidence:
  `C:\Users\00020271\Documents\ai-review-system-worktrees\agent2-daily-context-release-evidence\initial-dirty-tracked.patch`
- Initial tracked diff SHA-256:
  `26CBF342D9D16BE221EC1C15B589A1267FA5464F7B1F38CEA758C0C318B109A7`
- Clean baseline: `C:\Users\00020271\Documents\ai-review-system\.codex_tmp\baseline-fd887eca`
- Clean baseline HEAD: `fd887eca22c8686df3ca26319344b713ca304fab`
- Clean candidate: `C:\Users\00020271\Documents\ai-review-system-worktrees\agent2-daily-context-release-candidate`
- Candidate branch: `codex/agent2-daily-context-release-candidate`
- Candidate base: `fd887eca22c8686df3ca26319344b713ca304fab`
- Candidate commit: local freeze commit is recorded by the final handoff; this
  report is generated before that commit so the commit does not self-reference.

The original dirty worktree was not reset, overwritten, cleaned, staged, or
committed.  Only the clean candidate was changed.

## 2. Candidate behavior

The candidate adds or preserves the following behavior:

1. Explicit `write/open Daily` turns establish the Daily collection goal without
   writing the meta instruction into the report.
2. Plain asserted work inside an active Daily collection is handled before a
   competing internal-QA interpretation.
3. A single section update distinguishes missing, empty, and provided values;
   omitted sections are preserved.
4. A complete three-section document produces ordered typed commands whose
   working version advances after each command.
5. A correction such as “this is tomorrow's plan” moves the stable item ID
   rather than appending a duplicate.
6. A repeated correction is an explicit no-op: no report body upsert, no version
   increment, no false “changed” reply.
7. Scheduler reminders do not enter the user semantic chain or overwrite stale
   report body snapshots.
8. One turn can yield independent semantic facets.  Future self travel remains
   a Travel fact and is also projected to Daily `tomorrow_plan`, even without an
   active Daily goal.  Structured Daily content still exposes permission-scoped
   Case facts for independent Case processing.
9. Questions, hypotheticals, non-business future visits, and unresolved Case
   references do not acquire Daily or Case write authority.

The existing adversarial Travel test was deliberately strengthened after the
reviewer corrected the product rule: it now requires both
`record_travel_event` and `capture_daily_event(tomorrow_plan)`.  No test was
deleted, skipped, xfailed, or weakened.

## 3. Modified files and dependency boundary

Patch artifacts:

- Forward patch: `agent2_daily_context_release_forward.patch`
- Forward SHA-256:
  `632F020604D8C549723021805DFC773AAB7CB23345749FF79E4B923E0D6D3187`
- Rollback patch: `agent2_daily_context_release_rollback.patch`
- Rollback SHA-256:
  `E4217CD91AB12BAB2B43A329C41A93E131DAD8227D0709A5BECB75F35BF0C799`
- Round trip: the forward patch changed 19 candidate files in a detached
  base worktree; applying the rollback patch returned `git status --short` to
  zero entries.  The temporary verification worktree was then removed.

### Production code

- `app/agent2/cognitive_contract_v3.py`: closed contract support for the Daily
  mutation shapes used by the release candidate.
- `app/agent2/command_planner_v3.py`: ordered Daily command planning with an
  advancing working snapshot/version.
- `app/agent2/report_document_contract.py`: deterministic structured Daily
  parsing, section correction, and complete-document replacement.
- `app/agent2/runtime/context.py`: supplies the trusted Daily snapshot/item IDs
  required by corrections.
- `app/agent2/runtime/domains.py`: preserves simulated Daily receipts and
  command accounting.
- `app/agent2/runtime/harness.py`: validation/accounting for the added Daily
  command shapes.
- `app/agent2/semantic_interpreter_v3.py`: explicit Daily entry, active Daily
  priority, structured parsing, cross-facet Case/Travel preservation, and
  post-schema Travel metadata narrowing.
- `app/agent2/typed_daily_commands.py`: stable-ID move/replace/no-op semantics.
- `app/agent2/typed_daily_executor.py`: SQL executor no-op branch that does not
  upsert the report body or claim a change.
- `app/api/reports.py`, `app/api/webhook.py`, `app/stream_runner.py`: natural
  user-facing fail-closed messages without internal Agent2 pipeline terms.

### Evaluation and tests

- `app/agent2/evaluation/historical_daily_replay.py`
- `evals/agent2/dialogues/historical_daily_context_failures_20260721.json`
- `scripts/replay_agent2_historical_daily_context.py`
- `scripts/replay_agent2_daily_context_real_model.py`
- `tests/test_agent2_historical_daily_context_acceptance.py`
- `tests/test_agent2_daily_context_release_candidate.py`
- `tests/test_agent2_real_dialogue_adversarial.py` (strengthened Travel + Daily
  projection expectation)

No database schema, migration, lockfile, dependency, production default,
Admission policy, Receipt/Audit contract, permission rule, or external API
protocol was changed.

## 4. Clean baseline versus clean candidate

### Historical acceptance

- Clean baseline: 12 tests, 12 failed.
- Clean candidate final historical run: 12 tests, 12 passed.
- Candidate targeted release run: 20 passed.
- The two historical dialogues are retained as replay/acceptance fixtures; the
  repair is not keyed to a person's name or to one complete historical sentence.

### Related regression

- Baseline existing related suite: 199 passed.
- Candidate same existing related suite: 199 passed.
- Candidate expanded related suite: 218 passed.
- Candidate cross-domain and three-entrypoint suite: 208 passed.

### Full repository

- Clean baseline: 2406 passed, 65 failed, 2 skipped (2473 collected).
- Clean candidate: 2426 passed, 65 failed, 2 skipped (2493 collected).
- Candidate-only failures: 0.
- Base-only failures: 0.
- Failure node ID sets: identical.
- `python -m compileall -q app scripts tests`: exit 0.
- `git diff --check`: exit 0 (Git emitted only Windows line-ending notices).

Machine-readable evidence:

- `baseline_full.xml`
- `candidate_full_after_projection_contract.xml`
- `baseline_full_failure_nodes.txt`
- `candidate_full_after_projection_failure_nodes.txt`
- `candidate_cross_domain_and_entrypoint_regression_after_fix.xml`

## 5. PostgreSQL Repository gate

Status: **not executed / blocking condition**.

Read-only environment inspection found:

- `psql`: unavailable;
- `postgres` / `initdb`: unavailable;
- PostgreSQL Windows services: 0;
- Docker / Podman: unavailable;
- WSL executable exists but no installed distribution is available;
- `DATABASE_URL`, `TEST_DATABASE_URL`, `POSTGRES_DSN`: absent.

No network dependency was installed and no production database was contacted.
The candidate exercises the real typed SQL executor function with controlled
session/repository doubles, including a no-op assertion that forbids report
upsert and ConversationState mutation.  This is valuable code-path evidence,
but it is not represented as real PostgreSQL proof.

Before any Canary expansion, an isolated PostgreSQL run must prove:

- stable-ID move is one transaction and rolls back atomically;
- one-section updates preserve the other two sections;
- three commands advance versions consistently without partial commit;
- duplicate delivery is idempotent;
- no-op does not update the report row or version;
- reply content is built from the committed after snapshot.

## 6. Scheduler evidence

`test_actual_scheduler_entry_preserves_concurrent_daily_body` invokes the actual
`app.scheduler.jobs.remind_missing_reports` entry function.  A deterministic
barrier forces this order:

1. Scheduler reads a stale report proxy.
2. A user Daily command updates `tomorrow_plan` through the typed executor.
3. Scheduler resumes and may update reminder metadata only.

The test proves `today_work`, `problems`, the new `tomorrow_plan`, and report
version remain intact while `last_prompted_at` advances.  It uses controlled
repository/session doubles, not PostgreSQL; the database concurrency portion is
therefore included in the PostgreSQL gate above.

## 7. Bounded real-model evidence

- Model: `deepseek-v4-pro`
- Thinking: enabled
- Temperature: 0
- Timeout: 30 seconds
- Network retries: 2
- Semantic schema attempts: 3
- Stable system prompt SHA-256:
  `0fe769b72625097f917b7171dba01c3161f0b4b2a792d035a2301bee768a5ad7`
- Business/database/message side effects: 0

The retained baseline run was 15/18 and exposed three compound-correction
failures.  After the generic punctuation-boundary fix, the bounded six-case run
was 18/18.  A later Travel projection run produced five accepted results out of
six; the one non-accepted run had no semantic decision because the configured
model timed out after its retry budget.  The reviewer attributed current model
availability to the local VPN, so this is disclosed as environment instability,
not silently removed and not treated as a semantic P0.  Every successful result
for the Travel cases produced Travel plus Daily `tomorrow_plan`, never
`today_work`.

Raw outputs, hashes, latency, timeout, typed commands, and snapshots are retained
in the `real_model_*.json` artifacts.  No credential is stored.

## 8. Legacy `test_report_agent_state_protocol.py` failures

The requested “37 failures” does not match the current-date clean reproduction.
Both clean baseline and clean candidate collected 211 tests with exactly:

- 152 passed;
- 59 failed;
- 0 skipped.

The 59 failure node ID sets are byte-identical.  Candidate fixed none and added
none.  These tests exercise the legacy Agent1 `DailyReportService` protocol.
Webhook, Manual API, and Stream first resolve the runtime owner.  They reach the
legacy service only when route control chooses Agent1/legacy fallback; an
Agent2-primary identity does not use that path.  A person's display name alone
cannot establish whether yesterday's message used Agent1 or Agent2; the route
control audit/identity binding is authoritative.

## 9. User-visible wording and truthfulness

The public Webhook, Manual API, and Stream fail-closed messages no longer expose
“Agent2 typed command”, “did not fall back to Agent1”, Semantic Decision,
Planner, Executor, fail-closed, internal enums, stack traces, or technical error
details.  Internal logs and replay-only harness messages remain technical and
are not sent as production replies.

Success/no-op behavior is derived from executor results and after snapshots.
When before equals after, the SQL executor does not upsert and replies that the
content was already in the target section; it does not say “changed”.

## 10. Findings

- **P0: 0 open.**
- **P1 product defects: 0 open in exercised paths.**
- **P1 verification gap:** real PostgreSQL Repository/transaction path not
  executed; this blocks `GO_FOR_TWO_USER_CANARY` and any broader rollout.
- **P1 broader-rollout scope gap:** the reviewer-defined universal
  cross-domain Daily assessment is not yet implemented for pure Case and future
  domains outside an active/explicit Daily capture.  It does not invalidate the
  historical Daily fix, but broad rollout must not assume this capability.
- **P2:** one bounded real-model run timed out under the local VPN; the safe
  result was zero write.  No model parameters were changed.
- **P2:** Windows Git reports future LF-to-CRLF conversion notices; `diff
  --check` passes and no formatting rewrite was performed.

## 11. Feature defaults and rollout boundary

Production defaults remain disabled:

- `agent2_daily_enabled = false`
- `agent2_daily_enabled_user_ids = ""`
- `agent2_cognitive_core_v3_enabled = false`
- `agent2_business_phase2_enabled = false`
- Case/Travel write flags remain false.

Agent2 primary ownership is controlled by the existing tenant allowlist,
identity binding, and route-control Canary users.  The legacy Daily allowlist is
also exact-ID based.  Names must never be used as rollout identity and `*` must
not be used for the next expansion.

The product direction established during review is an independent semantic
facet model: Daily should assess eligible asserted work/risk/plan facts from
Case, Travel, and future tracking domains; Daily turns should also emit
independently grounded Case/Travel facets.  Each facet retains its own entity
resolution, Admission, command, and outcome.

This candidate proves only two directions of that model: explicit future
self-Travel is projected to Daily `tomorrow_plan`, and structured Daily items
with one visible Case reference can retain an independent Case facet.  It does
**not** yet implement a universal Case/other-domain-to-Daily assessment when no
Daily collection context exists.  The stable Prompt contract still treats a
pure Case progress statement as Case-only unless the user explicitly asks for a
Daily capture or an active Daily collection prompt is present.  That is a
newly clarified product-scope gap, not a regression introduced by this patch.
Broad rollout must not be described as already providing universal cross-domain
Daily absorption.

## 12. Required next steps before more users

1. Run the five PostgreSQL scenarios above against an isolated test database and
   retain before/after rows, receipts, versions, and rollback evidence.
2. Re-run the final targeted and full suites in the same build artifact used for
   the server candidate.
3. Identify additional volunteers by stable DingTalk/user IDs and verified
   tenant bindings; start with a small explicit set, never a wildcard.
4. Confirm route-control audit can prove Agent2 ownership for every test turn and
   preserve immediate Agent1 rollback.
5. Review the first real interactions for false success, section loss, Case
   misbinding, cross-facet contamination, and model timeout rate before a second
   expansion.
6. Specify and evaluate a safe cross-domain projection policy for pure Case and
   future tracking-domain facts.  It must distinguish asserted work/risk/plan
   from questions, hypotheticals, quotations, observations, and unresolved
   entities; this release candidate must not be extended with keyword routing.

No deployment, push, merge, service restart, production configuration change,
message send, production database access, or Canary expansion occurred.
