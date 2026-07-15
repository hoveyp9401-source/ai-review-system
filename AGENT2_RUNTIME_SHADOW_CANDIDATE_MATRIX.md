# Agent2 Runtime Phase 1 final evidence matrix

Final Runtime hash: `3aecf50254c165c33c26433df1c1eb940783a2d36774e5df98901cc8fb8ba52d`

| Workstream | Final local evidence | Status | Remaining decision condition |
| --- | --- | --- | --- |
| A. Evidence inventory | Requirements, oracle map, ADRs, manifests, replay and test commands are recorded | complete | None locally |
| B. Anti-oracle | Runtime dependency graph excludes evaluation; every `expected_*` and review/seal field is recursively rejected before the model seam | complete | None locally |
| C. Semantic interpreter | Closed schema, input limit, repair loop, nested executable rejection and fail-closed lifecycle tests pass | complete | Real-model accuracy still lacks human Gold |
| D. Independent tape | 1,407-record reviewer packet plus 73-row ledger packet; 0 human-approved | blocked | Independent reviewers must adjudicate and seal a high-risk Gold set |
| E. 42/29 ledger | Two same-identity runs: 42/42 have no write-intent candidate; A executes 22/29 and legally blocks 7/29, B executes 23/29 and blocks 6/29; no unknown root cause | locally classified | Independent closure count remains 0; the A/B state-path difference itself fails determinism |
| F. Adversarial/fuzz | 168 real-model cases and 600 structural seeds; actual write 0, legacy fallback 0, unexpected failed-closed 0 | safety invariants pass, semantic gate fails | Two active-goal write-intent candidates require independent adjudication |
| G. Determinism/state | Scripted serial/concurrent output is exact; real-model A/B share input hash, Runtime hash and run id | failed | 89/145 real-model turns differ; artifact hashes differ |
| H. Isolated DB | In-memory transaction/idempotency/version/owner/schema/rollback tests and structural fuzz pass | blocked | No isolated PostgreSQL/container is available for real DB smoke |
| I. Regression | Agent2/first-layer 944 passed; full repo 1,230 passed/142 failed; direct Runtime failures 0 | scoped pass | 136 legacy state-protocol and 6 Runtime-unrelated failures remain outside this gate |
| J. Shadow adapter | UUID identity, trusted evaluator, no-write copy, PII minimization, kill switch, sampling, timeout, failed-closed and log-failure circuit tests pass | offline complete | Online Runtime is not deployed; no online isolation/kill-switch/log proof |
| Final | All artifacts frozen under the final Runtime hash | `NO_GO` | Production Shadow and Live are prohibited |

The authoritative report is `outputs/AGENT2_RUNTIME_PHASE1_FINAL_ACCEPTANCE_REPORT.md`.
