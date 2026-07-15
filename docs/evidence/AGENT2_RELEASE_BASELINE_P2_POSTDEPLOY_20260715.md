# Agent2 Release Baseline P2 Post-Deploy Evidence

- Date: 2026-07-15
- Local release commit: `0319de1f`
- P2 verdict: `P2_API_AND_UI_PROJECTION_PASSED`
- Whole-goal verdict: not yet adjudicated; real-user P3 remains open

## Outcome

Legal Ops now presents the deployed two-user authorization contract instead of
making all 80 cases look like undifferentiated ownership. Pang Hao and Liu Cong
each see 40 `本人负责` cases and 40 `团队协作` cases. Both server-resolved
bindings contain 80 writable case IDs, so either user may create progress on a
team case. Existing progress remains editable or deletable only by its reporter.

The default travel view now removes acceptance-smoke and fixture records and no
longer mixes `case_progress_followup` notifications into the travel domain.

## Tests

| Gate | Result |
|---|---:|
| Local Legal Ops focused suite | 78 passed, 0 failed |
| Server staged focused suite | 35 passed, 0 failed |
| Full repository | 2407 passed, 59 failed, 1 skipped |
| New failures relative to the frozen P1 baseline | 0 |
| Inherited valid report-protocol blockers still open | 35 |

The 59 failures are the exact P1-adjudicated report-protocol set. They were not
deleted, skipped or xfailed. The machine comparison is stored locally under
`artifacts/agent2-release-baseline/p2`.

## Deployment and rollback evidence

- Transfer archive SHA-256:
  `baf3127cccca5af86b2d3801369956fd4ed8d8a27e89fee2f1ce507398397f88`
- Deployed runtime manifest SHA-256:
  `f49397532b09dfdef4052b66339e8c23bc4244e0b167604f20b325365981c840`
- Pre-deploy rollback archive SHA-256:
  `737ed5223f0c03e0120f2d31fecc97d1ac954e64b3685a0ea2010c9226a8ab60`
- Post-deploy processes:
  API `3536329`, Stream `3536331`, Scheduler `3536330`
- `/health`: `ok`

The first overlay attempt encountered a filesystem ownership mismatch before a
service restart. Every target file was then verified against the pre-deploy
manifest and all nine matched. The successful retry used the service owner's
SIGTERM permission and the units' declared `Restart=always` behavior to obtain
new PIDs. Any retry validation failure was configured to restore the targeted
rollback archive and recycle the same three units.

## Production read-only verification

| Evidence | Pang Hao | Liu Cong |
|---|---:|---:|
| Visible cases | 80 | 80 |
| Assigned to current user | 40 | 40 |
| Team collaboration | 40 | 40 |
| Writable cases | 80 | 80 |
| Reports visible | 27 | 17 |
| Default travel intents | 4 | 1 |
| Collaboration candidates | 1 | 1 |
| Travel notifications | 1 | 1 |

Both an assigned case and a collaboration case returned `can_add_progress=true`
for each credential. All default travel records were labelled as real user
messages. A missing credential and an invalid credential both returned HTTP
401. The differing report counts prove that shared case scope did not broaden
report visibility.

## Safety state

- Semantic Admission: enabled for review, enforcement off.
- Case Follow-up evaluation: enabled.
- Case Follow-up real sending: off.
- Case-to-report automatic projection: off.
- Canary remains the same Agent2 Sandbox tenant and the same two users.

## Observation not claimed as passed

The desktop in-app browser connection timed out and reset during post-deploy
navigation, so this evidence does not claim a successful automated visual login.
The public index did return `app.js?v=20`, the public script contained the new
assigned/shared/read-only presentation, and both production credentials passed
the authenticated API projection checks. A later stable browser run may add a
visual observation, but it is not substituted here with a fabricated screenshot.

P2 does not authorize proactive Follow-up sending, automatic report projection,
broader Canary routing, or Agent2 replacement of Agent1.
