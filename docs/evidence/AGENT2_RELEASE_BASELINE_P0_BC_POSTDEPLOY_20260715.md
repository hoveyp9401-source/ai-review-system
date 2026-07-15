# Agent2 Release Baseline P0-B/P0-C Post-deploy Evidence

## Verdict

`P0_BC_DEPLOYED_STRUCTURALLY_VALIDATED`

The message-ownership and anti-double-write slice is deployed and structurally
validated in production. This is not a two-user real-dialogue acceptance and
does not establish that Agent2 replaces Agent1.

## Deployment

- Local deployment commit:
  `d10410fd39538a155df4ff3c3713de5b78614ee8`.
- Production remains a separate Git history at
  `96cecec81c4ac9fe4e0500c08a52e661d094147d`; deployment used an explicit,
  hash-verified overlay rather than pretending the histories were identical.
- The nine runtime files matched their staged source byte-for-byte before
  startup. Runtime hash-manifest SHA-256:
  `3c1c945fdbf564b66394b869ec5e00accccc9567a722e49f6fb17db61b5516ea`.
- Pre-deploy files and all receipts remain in the mode-700 server snapshot
  `/home/ai_review_tunnel/codex_backups/agent2_p0bc_predeploy_20260715T132500Z`.

## Database authority

The first migration candidate correctly failed because the application role is
not the owner of the legacy `webhook_events` table. The transaction rolled back
and emitted a FAIL receipt; no ownership or legacy-table constraint was
bypassed.

The accepted design uses an application-owned `message_ingress_claims` ledger.
The final hash-pinned migration passed:

| Check | Result |
|---|---:|
| Migration SHA-256 | `e92991500e848a5121567bfee2e67151273b36e8b4ccb1f1db24e7fce20db464` |
| Public migration receipt SHA-256 | `440dbff71fad39548f110c3aee940839253d07a40fe6cb4a5e7bcbee9965b972` |
| Historical Webhook events / claims | 2,128 / 2,128 |
| Orphaned claims | 0 |
| Unclaimed Webhook events | 0 |
| Cross-transport duplicate groups | 0 |

The isolated PostgreSQL gate verified first apply, backfill, unique conflict
`23505`, idempotent replay and cleanup. The production rollback-only repository
smoke then created one synthetic claim/event in a transaction, replayed the
same provider message through the historical Stream key, received the original
event with no second insert, and rolled everything back. Persisted synthetic
claims and events were both 0. Smoke artifact SHA-256:
`3cf824d22619cbab99430422a8c92c2149746bc93d0c4bcad37c0c8aaf5efd39`.

## Runtime verification

At 13:46 CST, API, Stream and Scheduler were each active with one main PID.

- `/health`: `{"status":"ok"}`
- `/legal-ops/`: HTTP 200
- warning/error journal entries since startup: 0

The post-deploy read-only audit SHA-256 is
`a64a3cd8bfb281c811dcb31ea696a996451faeff6c173f496a82b7a88e5bc8a5`.
Business actual writes remained 145 and Webhook events remained 2,128 across
the deployment window, so the migration, overlay and rollback smoke added no
committed business write.

## Scope and switches

- business tenant count: 1;
- Canary users: 2;
- Semantic Admission: enabled for review, enforcement off;
- case follow-up evaluation: enabled;
- real follow-up send: off;
- automatic report projection: off;
- Agent1 rollback flag in the target route: off.

The 20-user report reminder allowlist remains a separate Agent1-era operational
scope. No Scheduler reminder audit exists for the personal-account messages
reported by the user, so those historical sends are not attributed to this
application.

## Remaining gates

1. Adjudicate the inherited 142 failures by current behavior contract; none was
   added by this deployment.
2. Complete UI truth-source minimum verification.
3. Run controlled Pang Hao/Liu Cong positive and negative real-dialogue cases.
4. Keep proactive follow-up send, report projection and Semantic Admission
   enforcement closed until their own gates pass.

