# ADR 0021: Report Date, Fact Preservation, and Regression Adjudication

- Status: Accepted
- Date: 2026-07-15
- Scope: Agent1 typed-daily compatibility path, Agent2 Agent Core operation records, report protocol release gates

## Context

The release baseline contained 142 inherited failures: 136 in the legacy report
state protocol suite and six in Agent Core. The largest common cause was not a
model-quality problem. The daily service silently made the previous reporting
day the default for every weekday message received before 09:00. This let old
report context capture current work and caused daily, weekly, monthly, case and
travel turns to compete for the same conversation.

At the Agent Core boundary, the daily presentation cleanup also removed date
anchors such as `今天` and `明天` from direct whole-turn facts. That changed the
fact stored in dry-run operations and replay artifacts. Missing report slots
could additionally capture short social replies, and a future marker anywhere
in a multi-clause turn could collapse the entire turn into one tomorrow-plan
item.

The old suite also used `report_saved` for two different facts: a committed
business-content write and persistence of conversation-only state. The release
Outcome Contract requires those facts to be separated and forbids a no-op from
being described as a business write.

## Decision

1. On a normal reporting weekday, the default report date is the local calendar
   date at receipt time. The before-09:00 window authorizes an explicitly
   requested previous-day operation; it does not silently change the default.
2. Saturday before 09:00 remains the one defaulting exception and resolves to
   Friday, the last required reporting day.
3. An explicit current-day fact takes precedence over a historical reference
   word in the same turn. A turn containing current and future facts must be
   processed as a multi-section turn, not collapsed into tomorrow plan.
4. When Agent Core receives one direct whole-turn daily fact, its operation
   command retains the exact trimmed user text, including time anchors. Extracted
   fragments from a multi-intent turn continue to use their bounded evidence
   spans.
5. Missing-slot fast paths must reject non-report phrases. Reference operations
   with no reference object and ambiguous edit/merge requests produce zero
   business writes and do not persist an unverified content candidate.
6. A resolved, persisted pending branch is authoritative before the LLM. An
   unknown pending type still fails closed in the state resolver.
7. Release adjudication compares exact JUnit testcase identities. A current
   failure that did not exist in the frozen baseline fails the adjudication
   command. Baseline failures are classified individually as:
   `fixed_valid_regression`, `unresolved_valid_regression`,
   `superseded_behavior_contract`, `superseded_outcome_semantics`,
   `accepted_security_strengthening`, or `test_fixture_drift`.
8. `report_saved` is retained only as a compatibility field. New acceptance
   evidence must use `OperationOutcome.actual_write`, committed receipt and
   audit references. No-op state persistence is not proof of a business write.
9. The 35 remaining valid report-protocol regressions remain blockers for their
   affected legacy flows. They are not deleted, skipped, xfailed, or declared
   passed. Agent1 remains in service for non-Canary users, so there is no blanket
   deprecation of its report suite.

## Consequences

- The focused report suite changed from 136 failures to 59 failures and 152
  passes. There are zero newly introduced testcase failures relative to the
  frozen baseline.
- All six Agent Core failures now pass because operation records preserve their
  direct business facts.
- Of the 142 frozen failures, 83 are fixed, 35 remain valid blockers, 15 assert
  a superseded behavior contract, four assert superseded outcome semantics,
  three have historical clock-fixture drift, and two expect a weaker security
  posture.
- This adjudication does not authorize broader Canary routing, proactive
  Follow-up sending, or automatic report projection. Those switches remain
  closed until their independent gates are satisfied.

## Verification

The machine-readable adjudication and both JUnit inputs are stored under
`artifacts/agent2-release-baseline/p1`. The adjudicator exits non-zero if the
baseline does not contain exactly 136 report failures, the total adjudicated
record count is not 142, or a new current failure appears.

## Rollback

Restore the pre-P1 code revision and restart the same API, Stream and Scheduler
deployment units from the recorded P0 snapshot. Do not alter the message-owner
ledger or P0 anti-double-write migration. Keep semantic admission enforcement,
Follow-up sending and report projection disabled during rollback.

