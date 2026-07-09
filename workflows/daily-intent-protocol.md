# Daily Intent Protocol

Status: first-priority draft.

## Purpose

Daily report intent recognition is now the first priority. The current system has too many intent decisions spread across direct rules, pending-state handling, LLM routing, executor guards, and date/history logic.

The goal is to make every daily-report decision pass through one explicit protocol frame before execution.

## Protocol

`DailyIntentFrame` is the canonical shape for daily-report intent.

Fields:

- workflow: always `daily_report`.
- operation: `fill`, `edit`, `confirm`, `query_current`, `query_history`, `copy_previous`, `clear`, `revoke`, `no_write`, or `unknown`.
- target_date: the report date or referenced history date.
- target_field: `today_work`, `problems`, `tomorrow_plan`, `all`, `none`, or `unknown`.
- target_items: 1-based item references.
- content: normalized content extracted from the user message or action plan.
- should_write: whether execution may mutate persisted daily-report data.
- needs_confirmation: whether a human confirmation is required before mutation.
- pending_relation: whether this decision sets, clears, or preserves pending state.
- confidence: high, medium, or low.
- source: direct rule, pending state, report agent, draft decision, or fallback.
- branch: the specific route branch.
- safety_flags: structural warnings that must be checked before execution.
- reason: concise explanation.

## First Implementation Slice

The first slice is observe-only:

- Existing daily-report execution stays unchanged.
- Existing `ActionPlan` decisions are projected into `DailyIntentFrame`.
- `report_service.py` writes the protocol projection into `daily_intent_*` timing fields after `DecisionRouter` returns.
- Timing/log payload must not contain raw user text.
- Tests validate the protocol mapping before it controls behavior.

Implemented files:

- `app/workflows/daily_intent.py`.
- `tests/test_daily_intent_protocol.py`.
- `app/services/report_service.py`.

## Why This Comes Before More Monthly Expansion

Daily report is the production foundation. Monthly report, travel coordination, case progress, Q&A, and legal research will all depend on reliable workflow ownership and state handling.

If the daily-report intent layer is not made explicit first, every new workflow increases the chance of cross-writing, stale pending state, or wrong-date edits.

## Migration Plan

1. Map existing `ActionPlan` into `DailyIntentFrame` in observe-only mode.
2. Add `DailyIntentFrame` timing fields for online smoke analysis.
3. Convert daily-report issue-ledger cases into expected intent frames.
4. Add pre-execution safety checks from `DailyIntentFrame`.
5. Move direct rules and pending-state branches to emit `DailyIntentFrame` directly.
6. Make executor consume `DailyIntentFrame` instead of raw action-plan ambiguity.
7. Remove duplicated intent checks only after online smoke is green.

## Required Safety Flags

Initial safety flags:

- low_confidence.
- write_without_actions.
- missing_target_field.
- multiple_target_fields.
- destructive_without_confirmation.
- non_write_operation.
- pending_transition.

These are not all fatal. They are gates for either confirmation, clarification, or online smoke assertions.

## Done For First Slice

- Protocol model exists.
- Current `ActionPlan` can be projected into protocol frames.
- Tests cover fill, multi-field fill, confirm, history query, destructive edit, and pending confirmation.
- No user-facing behavior changes.
