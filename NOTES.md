# Notes

## Department Collaboration Bot

The user no longer wants the DingTalk bot to be only a daily-report agent. The desired role is a department information-circulation assistant for the legal/contract center.

The existing daily-report bot remains important, but it should become one capability under a broader assistant. Existing daily-report behavior must be preserved, including normal submission, gradual submission, modification, recall, late submission, merge, history lookup, copy/edit/confirm flows, and understanding yesterday's unfinished or planned items before supplementing today's report.

## Capability Areas

- Daily report: collect, revise, recall, supplement, merge, query history, gradually fill, and reuse yesterday's relevant content.
- Monthly report: send metric data to team owners, collect replies, assemble each team monthly report, and aggregate department monthly report for leadership review.
- Travel collaboration: use daily reports or travel spreadsheets to detect overlapping travel plans and privately suggest coordination.
- Case progress collection: maintain source case sheets with plaintiff/defendant and collect progress from handlers through daily reports, scheduled reminders, or case milestone triggers.
- Q&A: answer internal questions through RAG or an internal knowledge base.
- Legal research: future capability; exact workflow still unclear.

## Product Direction

The bot should not become a pile of enum-controlled patches. It needs a role model, workflow router, domain-specific modules, durable ledgers, and testable data contracts.

The daily-report capability should remain the most mature workflow and become the reference implementation for later workflows.

Monthly report is the next real demand because the user is already testing team-level collection and leadership-level aggregation.

## Early Design Constraints

- Separate "what the user is trying to do" from "which workflow receives the message".
- Support partial replies, batch replies, edits, and confirmations across workflows.
- Keep raw incoming messages, parsed structured data, generated drafts, and final submitted artifacts separate.
- Use shadow/testing mode before sending new monthly-report or coordination messages to real leaders.
- Keep leadership-facing output short, clear, and filtered; do not dump all collected details.
- Prefer explicit data contracts over free-form prompt-only parsing.
- Preserve current daily-report behavior while refactoring the architecture.
