# Agent2 Release Baseline P0-B/P0-C Pre-deploy Evidence

## Result

The limited deployment candidate closes two latent production safety gaps:

- one DingTalk provider message now has one identity across Webhook and Stream;
- while the Phase 2 route plane is enabled, exactly one of Agent1 or Agent2 owns
  the message.

The production read-only audit found no historical cross-transport duplicate
group and no repeated business-write group. This means the defect was a latent
risk, not evidence of a known duplicate-write incident.

## Production facts before deployment

| Fact | Value |
|---|---:|
| Webhook events | 2,128 |
| Events still processing | 0 |
| Cross-transport duplicate groups | 0 |
| Repeated business-write groups | 0 |
| Business receipts / actual writes | 163 / 145 |
| Route audits | 104 |
| Configured business tenants / canary users | 1 / 2 |
| Explicit visible/writable cases per canary binding | 80 / 80 |
| Agent1 rollback enabled | No |
| Follow-up real send / automatic report projection | Off / Off |
| Semantic admission enforcement | Off |

The production audit artifact is stored in the restricted server snapshot and
has SHA-256
`0820c2d14526ad7175e6e8f6dc670c72667e31e81cc5a0de08624a3bfaa776c9`.

## Reminder attribution finding

Production has 20 users in the reminder allowlist and reminder sending is
enabled. However, `report_interaction_events` contains zero
`daily_report_reminder_sent` rows. Therefore the messages described as having
been sent from the user's personal DingTalk account cannot be attributed to the
application Scheduler from current evidence. The application code uses the
enterprise robot and work-notification APIs, not DingTalk MCP or a personal
account. An external MCP/manual send remains the evidence-consistent
explanation; it is not marked as proven without an external provider audit.

Future application reminders now retain the provider reference and state only
`accepted_by_provider`. A provider query/task reference is not called a message
ID, delivery confirmation, or user receipt.

## Regression evidence

- Focused safety, entrypoint, claim-ledger and reminder regression: 55 passed.
- Isolated PostgreSQL migration gate: passed; historical backfill, orphan
  detection, database uniqueness (`23505`), idempotent replay, and cleanup all
  passed. Gate artifact SHA-256:
  `0f8ec7d69674386043d17d29afefd6e76471d88d04cb0d66940a3129d9e3911b`.
- Full suite: 2,317 passed, 142 failed, 1 skipped.
- The set of 142 failures is byte-for-byte name-equivalent to the frozen
  baseline: 0 added and 0 removed.
- Full-suite JUnit SHA-256:
  `09d93e7f23dc9adc18b2b03cad644ba84a467108fedef168cd1718d53a6d1f55`.

The 142 inherited failures are not declared acceptable by this evidence. They
remain subject to the separate behavior-contract adjudication phase.

## Pre-deploy decision

`PASS_FOR_LIMITED_DEPLOYMENT_VALIDATION`

This is not a user acceptance or Agent1 replacement decision. Deployment must
still apply the application-owned ingress-claim migration, verify exact file hashes, keep the
two-user scope and all expansion switches closed, and collect post-deploy
read-only evidence.
