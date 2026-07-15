# Agent2 Semantic Admission Phase 1 — server Shadow evidence

Evidence time: 2026-07-14 (Asia/Shanghai)  
Deployment ID: `semantic_admission_20260714T005247Z`  
Target: current Agent2 server, repository `/home/ai_review_tunnel/ai-review-system`

## Verdict boundary

The Phase 1 implementation is deployed in **Shadow** mode. This evidence does not claim Enforce execution, a real Pang Hao/Liu Cong message closure, a business write caused by Admission, or Agent1 replacement.

## Release identity

- Runtime files: 46 exact paths.
- Runtime bundle SHA-256: `d20e3523f73671364f9187d89e8d609733abfd0f5bf83423d6a99e7bdf167031`.
- Release manifest: `artifacts/agent2-semantic-admission-phase1/release-manifest-final-20260714.json`.
- Server recomputed every file hash and the LF-canonical bundle hash after overlay.
- The remote repository was dirty before deployment. Deployment was path-allowlisted and did not use checkout, reset, or whole-repository copy.
- Pre-overlay files and `.env` are preserved under the protected deployment backup; backup directory mode is `0700`, environment backup mode is `0600`.

The first overlay attempt was stopped before migration, configuration change, or restart because the local canonical hash file used Windows CRLF while the server verifier used LF. Every individual file already matched. The original backup was retained, the canonical contract was corrected to LF, and the successful retry explicitly reused that original backup.

## PostgreSQL

Isolated-schema gates ran against the configured real PostgreSQL and cleaned their temporary schemas:

| Migration | SHA-256 | Isolated first/re-apply/rollback | Cleanup |
|---|---|---:|---:|
| Case Lifecycle / Outcome compatibility | `19b19a8653fdb4ba96bc2e300c886086b90a45982bb7d05b5cc7919718bdf34c` | PASS | PASS |
| Semantic Admission | `ac96c379ae93852d54896f60dc14213e9974c7bd760fe01490a9cea0d79c1aaa` | PASS | PASS |

The Case gate explicitly upgraded a synthetic legacy Outcome row, preserved it, added `object_label` and `object_version`, verified idempotent re-application and rollback, and proved the isolated run did not change `public`.

Both hash-pinned migrations were then applied to `public` and emitted redacted PASS receipts. Post-deployment inspection found all six Semantic Admission tables. At the inspection point all six contained zero rows, so no real-user Shadow event is claimed.

Structural violation counts were zero:

- Review not audit-only: 0;
- Deferred not audit-only/fresh: 0;
- invalid Ticket consumption: 0;
- forbidden raw Trace columns: 0.

`agent2_operation_outcomes.object_label` is non-null text; `object_version` is nullable integer.

## Effective configuration

Only aggregate scope is recorded; protected user IDs are not copied into this report.

| Control | Effective value |
|---|---:|
| tenant allowlist count | 1 |
| user allowlist count | 2 |
| Semantic Admission enabled | true |
| Enforce | false |
| Review capture | true |
| Deferred capture | false |
| Shadow replay | false |
| Case Follow-up send | false |
| automatic Report projection | false |
| `.env` mode | `0600` |

The tenant is the existing `sandbox-agent2-phase2-20260711`; the two-user set is copied from the existing protected Case Follow-up allowlists and is validated for equality without logging IDs.

## Runtime

- API: one production process;
- Stream: one production process;
- Scheduler: one production process;
- API health: `ok`;
- staging API on port 8010 was not modified.

The supervised restart script stopped the three production main processes and observed exactly one replacement for each. No parallel nohup worker was created.

## Blind result

One post-deployment label-free Blind attempt ran against the exact deployed Runtime content. It failed closed at proposal contract validation (`edit_daily_item` lacked a `daily_item_target`) before an actual artifact could be published. Labels were unavailable to the runner and scoring was not performed. The run was not retried to avoid selecting a favorable nondeterministic attempt.

Therefore `acceptance_eligible=false`, and Enforce/two-user Canary advancement remains blocked.

## Evidence files

- `artifacts/agent2-semantic-admission-phase1/server-deploy-20260714/shadow-deployment-evidence.json`
- `artifacts/agent2-semantic-admission-phase1/server-deploy-20260714/runtime-hash-after-overlay.json`
- `artifacts/agent2-semantic-admission-phase1/server-deploy-20260714/migration-case-public.json`
- `artifacts/agent2-semantic-admission-phase1/server-deploy-20260714/migration-semantic-public.json`
- `artifacts/agent2-semantic-admission-phase1/server-deploy-20260714/env-update-receipt.json`
- `artifacts/agent2-semantic-admission-phase1/server-deploy-20260714/deploy-status.json`
- `docs/evidence/agent2_case_lifecycle_postgres_gate_final_20260714.json`
- `docs/evidence/agent2_semantic_admission_postgres_gate_final_20260714.json`
- `evals/agent2/semantic_admission/actual_after_fix_failure.json`

## Operational conclusion

`SHADOW_ONLY`

The server may collect audit-only Shadow evidence for the scoped tenant/users. It must not issue authoritative Admission mutations, enable Deferred continuation, send Case Follow-up messages, or automatically project Case facts into reports. Real-user evidence, a scoreable frozen-runtime Blind cycle, and the open authority gaps must be closed before Enforce is reconsidered.
