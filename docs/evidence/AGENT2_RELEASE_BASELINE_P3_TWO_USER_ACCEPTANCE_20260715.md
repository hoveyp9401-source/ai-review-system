# Agent2 Release Baseline P3 — Two-user production acceptance evidence

Date: 2026-07-15 (Asia/Shanghai)

## Decision

`SHADOW_ONLY`

The current inbound two-user Canary remains available for Pang Hao and Liu Cong,
but P3 is **not** a two-user production acceptance pass.  Neither user has sent
an organic message after the runtime deployed at 2026-07-15 16:28:22 CST, and
Liu Cong has no successful real case, travel, daily or periodic write in the
read-only evidence window.  No simulated user reply is counted as human proof.

The proactive follow-up send switch and automatic report projection switch
remain off.  Canary scope was not expanded.

## Runtime and rollback identity

| Evidence | Value |
|---|---|
| Git commit | `7cc4b16eb18517c9b5c3915244f36979a14f3236` |
| Deployment archive SHA-256 | `976f10b61e4f8c05ea0ef3ecc810c1c126aea2b3faf420fe9769bcc73985c535` |
| Pre-deploy rollback archive | `/home/ai_review_tunnel/deploy-backups/agent2-p3-travel-dedup-predeploy-20260715T162244.tar.gz` |
| Rollback archive SHA-256 | `39dcc1fa6d582f5ae5a5b5a0259dfdba063ffaf2772876480947d46a39cd90c3` |
| API / Stream / Scheduler PIDs | `3553776` / `3553773` / `3553779` |
| Health | `GET /health` returned `{"status":"ok"}` |

The four deployed runtime files match the local commit byte-for-byte:

| Runtime file | SHA-256 |
|---|---|
| `app/agent2/admission_store_sql.py` | `1575a22e89858c1baeb3468fb6a3fa7b2e3760ed087f9dfb44f8ed5a4b1dab40` |
| `app/agent2/business/contracts.py` | `9d5db9dc04ba34748cf688321fe10b9a48af9021c36a3bda0594be16ad7c9531` |
| `app/agent2/business/executor.py` | `01dcd2abc3f5ff4799e03c0846ce1bac64851f88209dacc50d4a2b347d2bfd01` |
| `app/agent2/business/sql_executor.py` | `01b409644563cbe8c8218c9d1f0a91d150874eb196783c52b5fbb510cb912e53` |

## Newly discovered production defect and correction

Read-only history found that one identical Pang Hao message had been sent with
two provider message IDs.  The old runtime used provider-message identity as the
only idempotency boundary and therefore created two active `TravelIntent` rows.

The correction separates two cases:

1. replay of the same provider message returns the original receipt;
2. a different provider message asserting the same tenant/user/city/exact
   interval/purpose returns a new auditable `duplicate` receipt with
   `actual_write=false` and reuses the existing travel object.

The PostgreSQL unique travel fact key closes concurrent first-write races.  A
cancelled intent releases that fact key, so a later explicit re-registration is
possible without reactivating the cancelled row.

The one existing production duplicate was reconciled by typed
`UpdateTravelIntent(status=cancelled)`, not by SQL deletion.  Evidence:

- original intent ref: `c1d852642562b7ed`;
- cancelled duplicate ref: `eeab05f1e014ecbe`;
- reconciliation receipt ref: `a5f26c2818ac96ae`;
- receipt: `executed`, `actual_write=true`;
- audit present: yes;
- collaboration candidates/outbox entries on the cancelled duplicate: 0 / 0;
- post-correction active duplicate fact count: 0.

## Test evidence

| Layer | Result |
|---|---|
| Local travel/business/Admission/Outcome focused regression | 130 passed, 1 opt-in PostgreSQL test skipped |
| Server staged focused regression | 105 passed |
| Server deployed focused regression | 105 passed |
| Real server PostgreSQL rollback test | 1 passed |
| Full repository | 2410 passed, 59 failed, 2 skipped |

The real PostgreSQL test used two distinct source messages and proved one travel
object, two receipts, a no-write duplicate receipt, two audit events, safe
cancel/re-register behavior, and zero rows remaining after transaction rollback.

The full-suite JUnit SHA-256 is
`d5d37c6ba5de1b1df16deb61a2122bb86fd581ca4f5fa9adbc1af59a35bd40b3`.
Its 59 failure identities exactly equal the frozen P1 59-test set: 0 new and 0
removed.  The previously adjudicated 35 valid report-protocol blockers remain;
they were not hidden, skipped or reclassified by this change.

## Read-only real-user evidence

The safe evidence artifact is
`artifacts/agent2-release-baseline/p3/two-user-real-dialogue-postdeploy-20260715.json`
with SHA-256
`93a6c87becba15e33f3ef32c756e742c0e931a0d8f61f9a3d54307c5d2327395`.
It was collected under `SET TRANSACTION READ ONLY`; message and business text is
hashed by default.

| User | Provider-shaped messages | Real writes observed | Latest event (UTC) | Current-runtime human closure |
|---|---:|---|---|---|
| Liu Cong | 12 | no case/travel/report projection; no daily/periodic report in the window | 2026-07-15 01:03:31 | missing |
| Pang Hao | 34 | three case-progress rows, two travel rows before reconciliation, three daily and two periodic reports | 2026-07-15 02:13:24 | missing after deployment |

Pang Hao's historical writes prove that earlier runtimes reached PostgreSQL, but
they do not prove the newly deployed runtime.  Liu Cong's older failed attempts
are negative incident evidence, not a successful acceptance result.

## Switch evidence after deployment

| Switch | Value |
|---|---|
| semantic admission enabled | true |
| semantic admission enforced | false |
| case-progress write | true |
| travel write | true |
| case follow-up policy | true |
| case follow-up real send | false |
| case follow-up report projection | false |

## Remaining blockers

1. Liu Cong must complete current-runtime organic positive and negative flows;
2. Pang Hao must complete at least one current-runtime organic flow after this deployment;
3. the 35 valid report-protocol failures remain release blockers;
4. proactive follow-up send and automatic report projection have no two-user real closure;
5. therefore neither `READY_FOR_TWO_USER_CANARY` nor
   `TWO_USER_CANARY_VALIDATED` is justified.

