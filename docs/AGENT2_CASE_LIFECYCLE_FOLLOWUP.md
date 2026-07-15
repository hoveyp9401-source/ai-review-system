# Agent2 Case Lifecycle Follow-up and Report Projection

## Scope

This subsystem is fenced to the configured Sandbox tenant, user allowlist and the
exact union of Case IDs in those users' active identity bindings. A tenant-wide
Case scan is forbidden. Task generation, message dispatch and automatic report
projection have independent, default-closed effect switches.

## Authoritative flow

```text
committed Case / hearing / cadence fact
  -> CaseFollowupPolicy evaluation
  -> persistent CaseFollowupTask
  -> idempotent NotificationOutbox
  -> provider acceptance with provider message ID
  -> CaseFollowupPending + focused Task Ledger entry
  -> permission/version/conversation checked reply
  -> typed Case command + committed receipt
  -> persistent ReportProjectionRequest
  -> ReportProjectionPolicy
  -> unified Report executor
  -> independent Case and Report Outcomes
  -> receipt-only ReplyOrchestrator
```

The semantic model extracts candidate facts and evidence spans. It cannot select
database IDs, declare a write successful, declare a message delivered, or decide
the final projection eligibility. The Case write is primary. Report projection is
derived and cannot roll back or mutate the committed Case fact.

## Persistent contracts

- `agent2_case_lifecycle_states`: current stage, node, status, next actions,
  hearing readiness and blocking issues for one assigned Case.
- `agent2_case_followup_policies`: versioned effective cadence and event policy.
- `agent2_case_followup_tasks`: task lifecycle, message lifecycle and response
  lifecycle stored separately.
- `agent2_case_followup_pendings`: bounded Case Follow-up, Selection,
  Confirmation and Information pending records.
- `agent2_task_ledger`: focused, active and suspended task state. A Follow-up
  records the exact interrupted report task and version before it can restore it.
- `agent2_report_projection_requests`: durable hand-off after a committed Case
  receipt.
- `agent2_case_report_projections`: stable Case fact to Report item relation for
  exact correction or removal.
- `agent2_operation_outcomes`: authoritative business/message result used by
  the reply layer.

## Closed states

Task: `scheduled`, `queued`, `sending`, `waiting_for_reply`, `answered`,
`snoozed`, `cancelled`, `expired`, `failed`.

Message: `scheduled`, `queued`, `sending`, `accepted_by_provider`,
`delivery_confirmed`, `failed`, `cancelled`.

Response: `not_requested`, `awaiting_input`, `answered`, `snoozed`,
`cancelled`, `expired`.

`accepted_by_provider` is never presented as delivered, read or agreed.
`delivery_confirmed` requires a reliable provider callback.

## Trigger policy

Priority is hearing proximity, committed stage transition, allowlisted node
transition, manual trigger, then fixed cadence. Trigger event IDs and task
idempotency keys are stable. Due triggers inside one evaluation window are
merged into one task while retaining all trigger sources.

Cadence is calculated in the policy timezone from the latest meaningful
progress. Daily, seven-day, fifteen-day, calendar-month and custom-day
intervals are supported. A meaningful progress receipt recalculates the next
due time and invalidates obsolete cadence tasks. Waiting tasks suppress new
equivalent tasks. Reminders use a separate per-attempt idempotency key, interval,
maximum count and daily user/Case limit.

Stage and node triggers can only be produced from an executed
`create_case_progress` receipt with `actual_write=true`. Plaintiff stages are
`拟诉 / 诉讼中 / 执行中 / 已结案`; Defendant stages are
`受理 / 开庭 / 审结 / 履行 / 已结案`. Unknown values fail closed.

## Report projection

High confidence requires a unique Case, current user actor, explicit completed
work today or an explicit future action, an open writable report, no duplicate
relation and no user opt-out. Medium confidence creates Confirmation Pending.
Status-only replies do not project. Corrections target the stable projection and
Report item; removing or rewriting the Report item never deletes or rewrites the
Case progress.

## Operational switches

```text
CASE_FOLLOWUP_ENABLED
CASE_FOLLOWUP_SEND_ENABLED
CASE_FOLLOWUP_REPORT_PROJECTION_ENABLED
CASE_FOLLOWUP_TENANT_ALLOWLIST
CASE_FOLLOWUP_USER_ALLOWLIST
CASE_FOLLOWUP_TRIGGER_ALLOWLIST
CASE_FOLLOWUP_CONVERSATION_MAP_JSON
CASE_FOLLOWUP_DAILY_LIMIT
CASE_FOLLOWUP_CASE_DAILY_LIMIT
CASE_FOLLOWUP_REMINDER_INTERVAL_HOURS
CASE_FOLLOWUP_MAX_REMINDERS
```

Deployment starts with evaluation enabled and both effect switches closed.

