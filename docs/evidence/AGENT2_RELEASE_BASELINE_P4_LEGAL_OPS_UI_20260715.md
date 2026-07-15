# Agent2 Release Baseline P4 — Legal Ops UI truth evidence

Date: 2026-07-15 (Asia/Shanghai)

## Decision

`READY_FOR_TWO_USER_UI_CANARY`

This is an UI-only readiness decision. It does not supersede the P3 overall
`SHADOW_ONLY` decision and does not mean that Agent2 replaces Agent1. Pang Hao
and Liu Cong have not both completed a post-deployment human browser session,
so `TWO_USER_UI_CANARY_VALIDATED` is not justified.

## What changed

1. Case cards and case details expose court, cause, hearing time and risk level
   only when those values actually exist in a whitelisted source field. The 80
   current real-source cases do not contain those fields, so the UI no longer
   fabricates four uniform placeholders.
2. The follow-up surface now distinguishes task generation from DingTalk
   sending. With the current switches it says `创建追问任务（暂不发送）` and
   `当前仅生成追问任务，钉钉发送未开启`.
3. The team center declares that member identity comes from the Agent2 identity
   binding table and is a current-tenant/current-team server mapping. It no
   longer presents a hard-coded organization name as a business fact.
4. Browser title, initial shell text and reachable audit labels are Chinese.
   Raw source-message, receipt, audit, provider-message and resource IDs are not
   rendered on the product pages.
5. The unsupported risk-level bulk filter was removed. Existing real filters,
   preview and confirmed apply behavior remain.

## Data-source matrix

| Surface | Runtime source | Current server evidence | UI treatment |
|---|---|---:|---|
| Overview | PostgreSQL workspace aggregates | 80 visible cases | permission-scoped metrics with drill-down |
| Reports | daily and periodic report repositories | 44 admin-visible; owner scopes 27 / 17 | full user-visible report; internal version only used for optimistic write |
| Cases | `Agent2Case` read model | 80 `real_case_workbook`; 3 fixtures hidden | real source label; absent source fields omitted |
| Case progress | `CaseProgress` plus receipt/audit projection | typed add/update/delete endpoints deployed | user text preserved; internal IDs hidden |
| Team | active `Agent2IdentityBinding` plus real aggregates | 2 active members | explicit server-mapping notice |
| Travel | `TravelIntent`, candidates and notification outbox | 5 visible travel rows | provider acceptance and delivery confirmation remain distinct |
| Audit | receipts and audits | admin-only route | business labels only; raw identifiers suppressed |
| Follow-up | policy/task repositories plus runtime switches | policy enabled; send off; projection off | task creation is not described as message sending |

## Action matrix

| UI action | Real effect | Current disposition |
|---|---|---|
| Login | server credential authentication | enabled; invalid credential returns 401 |
| Refresh / overview drill-down | live GET and route/filter change | enabled |
| Case search and stage/type/progress filters | live permission-scoped query | enabled |
| Add case progress | typed authenticated case command | enabled for the two case-owner scopes |
| Edit/delete case progress | optimistic-version typed command | enabled only for records editable by the current actor |
| Add/edit/delete/submit report | unified report command endpoint | enabled only when the report action contract permits it |
| Save follow-up policy | typed admin policy command | administrator only |
| Create follow-up task now | creates an auditable task | administrator only; UI explicitly says that DingTalk sending is off |
| Bulk follow-up change | preview, explicit confirmation, per-case result | administrator only |
| DingTalk follow-up send | external side effect | off; no UI success claim |
| Automatic report projection | derived report write | off; no UI success claim |

## Test evidence

| Layer | Result |
|---|---|
| Local Legal Ops focused suite | 58 passed |
| Final server product UI suite | 15 passed |
| Full repository | 2412 passed, 59 failed, 2 skipped |

The final full-suite JUnit is
`artifacts/agent2-release-baseline/p4/full-suite-ui-truth-reviewed-20260715.xml`
with SHA-256
`c8f4dc2b1f884d398ecd08d7e1a5ee2830c15107fed1e71b4300065e0cb8e995`.
The 59 failure identities exactly equal the frozen P1/P3 set: zero added and
zero removed. They remain the previously adjudicated report-protocol blockers.

## Deployment and rollback identity

| Evidence | Value |
|---|---|
| Final Git commit | `0091e27b050ea52df2f2b44c555c409870d6b275` |
| Core UI-truth commit | `89eb32a9e10f9267e5f6305c1da5730a516a7d16` |
| Core package SHA-256 | `76d9d22f0cd2aa4f0b17d598a540e312b6b6a19aca33c1b47c98581c324b309d` |
| Final reviewed package SHA-256 | `59a0978282202d99c965dc0949e0066f94f522ddff46e2634cefb3d6782ed56a` |
| Core deploy directory | `/home/ai_review_tunnel/deployments/agent2-p4-ui-truth-20260715T165825` |
| Final deploy directory | `/home/ai_review_tunnel/deployments/agent2-p4-ui-truth-reviewed-20260715T171429` |
| Core rollback archive SHA-256 | `5d3ff6ed5c337e3e400a6b59455e40737fc0db6cd24d6a67460ded76da2a2428` |
| Final rollback archive SHA-256 | `acdc58155de59ee38a4c88feade91b081504908cba62d1df110b1db99e2c795d` |
| API / Stream / Scheduler PIDs | `3566788` / `3566786` / `3566789` |
| Public page | `http://124.221.205.13:8000/legal-ops/` returned 200 |

Current deployed file hashes:

| File | SHA-256 |
|---|---|
| `app/legal_ops/api.py` | `a13eb4879f1781748ca3bef005108f851108e6d461051409f2b31027ac57ede4` |
| `app/legal_ops/live_workspace.py` | `002ebb0d8d271d8a3690e74f24b75c48dc534a92918c8fe6ae42276556011d9a` |
| `app/legal_ops/static/app.js` | `e825a01fb29baa1863c0bbf304519d45ab9a162ddfccdac68cb5e3a11d9dd41c` |
| `app/legal_ops/static/index.html` | `7234285844a61240034d803eea89dc98acb859bd36f8950b00d8c9ebeceeddbb` |

To roll back all P4 changes, restore each P4 rollback archive in reverse deploy
order, ending with the core rollback archive, then run the supervised production
restart script. Every archive was created before its corresponding install.

## Credential and permission smoke

The server contains three active credential principals. Only credential hashes
are recorded here:

| Credential ref | Role | Cases | Assigned / shared | Writable | Reports | Follow-up management |
|---|---|---:|---:|---:|---:|---|
| `9b69d4c2878a` | tenant administrator | 80 | 0 / 0 | 0 | 44 | yes |
| `fbab13dbd3c0` | case owner | 80 | 40 / 40 | 80 | 27 | no |
| `f643c40be30c` | case owner | 80 | 40 / 40 | 80 | 17 | no |

Both owner credentials can add progress across the 80-case explicit shared
scope, but their report endpoints remain owner-scoped. An invalid credential
returned 401.

## Remaining evidence gap

- No human browser session by both Pang Hao and Liu Cong was counted after this
  deployment.
- No visual click-through was fabricated when the in-app browser control was
  unavailable.
- The P3 real-conversation and report-protocol blockers remain unchanged.
- Follow-up real send and automatic report projection remain off.
