# Daily Report Stability Program

Status: draft.

## Purpose

Daily report is the production-critical workflow. The broader department collaboration bot can expand only if daily report remains trustworthy.

This program runs in parallel with the new workflow-router foundation. Its purpose is to turn every daily-report pit into a durable test, ledger row, and structural fix.

## Principle

Do not treat daily report bugs as isolated phrase patches.

Every issue should be classified as one of:

- workflow routing error.
- pending-state error.
- executor guard missing.
- report-date or cutoff error.
- history/query/copy error.
- edit/merge/delete/clear error.
- confirmation or recall error.
- ASR or correction error.
- renderer or reply wording error.
- concurrency/idempotency error.

The fix should land at the narrowest structural layer that prevents the whole class of issue.

## Daily Triage Loop

Trigger:

- User reports a filling problem.
- Morning check finds missing, wrong, or blocked reports.
- Online smoke finds a regression.
- A new monthly-report route could conflict with daily report.

Steps:

1. Check production/server evidence first.
2. Identify exact user, date, message, reply, and persisted DB result.
3. Add or update `REPORT_ISSUE_LEDGER.md`.
4. Convert the issue into at least one golden fixture.
5. Generate about three equivalent wording variants.
6. Fix the structural cause.
7. Run targeted tests.
8. Run online daily-report smoke and issue-ledger smoke.
9. Only then mark the issue as fixed.

## Quality Gate

No new workflow should take over real traffic unless daily report passes:

- Existing daily-report issue-ledger smoke.
- Online system smoke.
- Product quick gate.
- Progress gate.
- Targeted tests for the touched route/state/executor layer.

The new workflow router can stay observe-only while daily report is unstable.

## Severity

P0:

- User content is written to the wrong person's report.
- Monthly/performance content is written into daily report.
- Clear/delete/recall affects the wrong date or wrong user.
- Bot confirms completion when the report was not actually saved.

P1:

- Correct user/date, but wrong field.
- History/copy/edit flow corrupts a draft.
- User is blocked in a pending state and cannot continue.
- Report-date cutoff applies incorrectly.

P2:

- Reply wording is confusing but data is safe.
- Bot asks unnecessary clarification.
- Renderer formatting is poor but content is recoverable.

P3:

- Cosmetic text, spacing, or minor phrasing issue.

## Release Rule

For daily report:

- P0/P1 fixes require online verification before any "fixed" claim.
- P2 can ship with targeted tests and next-smoke coverage.
- P3 can batch with normal cleanup.

For the department collaboration bot:

- Monthly-report real traffic can start only when no open P0/P1 daily-report issue is active.
- WorkflowRouter can observe while daily report is unstable.
- WorkflowRouter can take over one narrow path only after the daily-report gate is green.

## Recommended First Stabilization Target

Before expanding to more workflows, stabilize these daily-report classes:

1. Active monthly/performance reply must never fall into daily report.
2. Ambiguous text under an active non-daily task should not write anywhere.
3. Yesterday/today date cutoff must be deterministic.
4. Edit/confirm/delete/clear must always bind to the intended date and draft.
5. Copy-history and previous-plan rollover must preserve section boundaries.

