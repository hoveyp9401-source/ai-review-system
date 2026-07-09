# Daily Report V2 Workflow Spec

Status: draft.

## Purpose

Keep the current daily-report workflow stable while turning it into the reference implementation for the broader department collaboration bot.

This workflow is production-critical. It should be improved through guarded refactoring, not replaced all at once.

## Scope

Daily report v2 must preserve all existing user-facing capabilities:

- Normal filling.
- Gradual filling.
- Modify today's draft.
- Recall or clear submitted content where supported.
- Late submission.
- Merge new content into an existing draft.
- Query history.
- Copy or reuse yesterday's report.
- Understand yesterday's unfinished items or plans and supplement today's content.
- Confirm before final submission when needed.

Daily report v2 must also avoid absorbing messages that belong to monthly report, case progress, travel coordination, or Q&A.

## Trigger

The workflow can be triggered by:

- A user sending daily work content.
- A user sending a daily-report command, such as modify, clear, confirm, query, copy, or supplement.
- A scheduled reminder or scheduled daily collection.
- A late-submission request.

## Workflow Ownership Rules

Daily report owns a message when:

- The user is in an active daily-report conversation.
- The message explicitly references today's work, problem/risk, tomorrow's plan, confirmation, modification, recall, or history query.
- The message matches a known daily-report operation and no higher-priority active monthly/case/travel task is waiting for reply.

Daily report should not own a message when:

- The user is replying to an active monthly-report metric prompt.
- The message is clearly structured around metric fields such as unfinished reason/existing problem, next-month target, or action plan for numbered indicators.
- The message is a case progress reply to a case collection task.
- The message is a direct Q&A question and no daily-report state is active.

## Data Contract

Daily-report intent recognition is the first priority. All daily-report decisions should converge on `DailyIntentFrame` before the executor mutates state.

### DailyIntentFrame

- workflow.
- operation.
- target_date.
- target_field.
- target_items.
- content.
- should_write.
- needs_confirmation.
- pending_relation.
- confidence.
- source.
- branch.
- safety_flags.
- reason.

### DailyReportIntent

- sender_id.
- sender_name.
- report_date.
- operation.
- confidence.
- raw_text.
- extracted_today_work.
- extracted_problem_risk.
- extracted_tomorrow_plan.
- referenced_history_date.
- needs_confirmation.
- reason_for_route.

### DailyReportDraft

- sender_id.
- report_date.
- today_work.
- problem_risk.
- tomorrow_plan.
- source_message_ids.
- revision_count.
- last_updated_at.
- status.

### DailyReportOperationLedger

- operation_id.
- sender_id.
- report_date.
- operation.
- before_snapshot.
- after_snapshot.
- raw_message.
- parser_version.
- result.
- created_at.

## State Machine

Main states:

- idle.
- collecting.
- draft_ready.
- awaiting_confirmation.
- submitted.
- editing.
- recalled_or_cleared.

Important transitions:

- idle -> collecting: first daily content received.
- collecting -> draft_ready: enough content parsed.
- draft_ready -> awaiting_confirmation: bot asks user to confirm.
- awaiting_confirmation -> submitted: user confirms.
- submitted -> editing: user asks to modify or supplement.
- editing -> draft_ready: bot regenerates draft.
- submitted -> recalled_or_cleared: user explicitly clears or recalls.

## Validation

Daily-report validation should catch:

- Empty daily report.
- Message routed to daily report while an active monthly task is waiting.
- Tomorrow-plan text accidentally written into today's work.
- Problem/risk text accidentally written into tomorrow's plan.
- "Yesterday completed" style text preserving stale future shell.
- Confirm/modify/clear commands acting on the wrong date.

## Testing Gate

Before any daily-report v2 change is considered safe:

- Existing unit tests pass.
- Existing issue-ledger tests pass.
- Online daily-report smoke tests pass.
- Historical difficult cases for Lu Jian, Pang Hao, Yao Jinghua, and other known users are included as fixtures if available.
- At least one test proves a monthly-report reply is not written into daily report.

## Implementation Sequence

1. Add message envelope without changing behavior.
2. Add workflow router in observe-only mode.
3. Compare old routing and new routing in logs.
4. Enable router only for monthly-report task replies.
5. Move daily-report operations behind explicit DailyReportIntent.
6. Expand fixtures for previously fixed issues.
7. Only then clean up older ad hoc parsing branches.
