# Concurrency And Idempotency Review

Date: 2026-06-15

## Scope

This review covers the current production code paths for:

- DingTalk stream message ingestion
- daily report upsert
- duplicate DingTalk message handling
- batch reminders
- scheduled jobs

No database schema change is proposed here.

## Existing Protections

1. Per-user daily report uniqueness

`daily_reports` has a unique constraint on `(user_id, date)`.

Impact:

- one user should not create multiple report rows for the same report date
- upsert logic targets the existing row for the same user and date

2. DingTalk webhook idempotency

`webhook_events.idempotency_key` is unique.

Impact:

- repeated DingTalk callbacks with the same message id are deduplicated
- stream handler returns cached response payload for duplicate events

3. Stream queue and worker pool

`app.stream_runner` uses:

- bounded queue: `stream_queue_size`
- worker pool: `stream_worker_count`
- per-message processing timeout
- reply timeout

Impact:

- 70 users sending messages at the same time should be within the intended scale
- overload returns a polite queue-full response instead of blocking forever

4. Reminder job grouping

Reminder sending groups users by team and reminder text.

Impact:

- group robot mode can mention multiple users with the same reminder state
- direct robot mode sends separate grouped batches by text

5. Scheduler max instances

Scheduler jobs use `max_instances=1`.

Impact:

- the same scheduled job will not overlap with itself if a previous run is still active

## Current Risks

1. Concurrent messages from the same user can still race

Two different messages from the same user on the same report date can be processed by different stream workers at the same time.

Potential impact:

- both workers load the same existing report state
- later commit can overwrite or merge from stale state
- field order may not match arrival order

Current mitigation:

- `merge_ordered` reduces duplicate sentence pollution
- daily row uniqueness prevents duplicate report rows

Recommended minimal fix later:

- lock the daily report row during update, or
- serialize processing per `(user_id, report_date)` in the stream runner

2. Insert race for a brand-new report

If two first messages from the same user/date arrive together, both may observe no report before one inserts.

Potential impact:

- database unique constraint prevents duplicate rows
- one transaction may fail unless handled by retry/upsert-at-SQL level

Recommended minimal fix later:

- make `upsert_daily_report` use database-level insert-on-conflict, or
- catch unique violation and reload/merge once

3. Reminder send result is not persisted

The system returns send counts but does not persist per-user reminder success/failure.

Potential impact:

- hard to audit who actually received a reminder
- retry strategy is limited

Recommended minimal fix later:

- add a lightweight reminder log table only after product behavior is stable
- before schema change, keep structured logs around reminder results

4. Scheduler process is not currently part of the documented production process list

Current documented long-running processes are API and stream_runner.

Potential impact:

- reminder strategy code exists, but automatic scheduled reminders will not run unless scheduler is started separately

Recommended minimal fix later:

- decide whether scheduler should become a third managed process
- ideally manage API, stream_runner, and scheduler with systemd

## Tests To Add Next

1. Duplicate DingTalk stream message returns cached response and does not submit twice.
2. Same user sends two messages quickly; final report preserves both useful fragments.
3. Two users submit at the same time; reports do not cross users.
4. First-message insert race for one user/date is retried or handled.
5. Reminder grouping sends the expected text per user state.
6. Auto-submit job only completes due pending reports.

## Summary

The current design is acceptable for early 70-person internal testing, with useful safeguards already in place:

- unique daily report per user/date
- unique webhook idempotency key
- bounded stream queue
- worker timeout controls

The main remaining engineering risk is same-user concurrent message ordering and stale-state merging. That should be handled before broad rollout, but it does not require a database migration as the first step.
