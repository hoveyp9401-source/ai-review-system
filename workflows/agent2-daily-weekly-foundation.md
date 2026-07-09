# Agent 2.0 Daily / Weekly Foundation

Status: Phase 1.1 implementation slice.

## Goal

Agent 2.0 starts as a safer intake layer for daily and weekly reporting. It must prove that the new architecture is more stable than the old daily-report fallback before it controls writes.

This slice is observe-only. It builds a structured routing plan and safety decision, but existing daily-report and monthly-report execution remains unchanged.

## Current Scope

- Daily report ownership.
- Weekly report ownership.
- Monthly report guardrails, so metric replies do not fall into daily report.
- Internal Q&A recognition, so questions do not enter daily report.
- Travel wording as a multi-workflow example, because travel can also create daily-plan items.
- Case progress observe-only recognition.
- Legal research observe-only recognition.
- Legacy daily protective gate.

## Public Contract

`WorkflowRouter.plan(envelope)` returns a `RoutingPlan`.

The plan contains:

- `primary_workflow`: the main owner of the message.
- `matched_workflows`: all workflows hit by the message.
- `effects`: planned business impacts, not execution commands.
- `safety_decision`: whether the plan is blocked, allowed, or needs confirmation.
- `signals`: scoring details for audit and smoke tests.
- `segments`: sentence-level workflow ownership for multi-intent messages.
- `observe_only`: always true in this slice.

`segments` is important because workflow ownership is not a single-choice
problem. A single DingTalk message may contain daily-report content, small talk,
and a legal-research question. Agent 2.0 records each segment separately so the
future executor can decide what to write, what to answer, and what to ignore
without collapsing everything into the old daily-report fallback.

## Safety Rules In This Slice

- Default mode is `observe_only`; it records the decision and does not change legacy daily behavior.
- `protective_gate` mode blocks messages before the legacy daily executor when the message is clearly not daily, unsafe, or multi-effect.
- `strict_gate` exists as an interface only and should not be enabled for production daily execution yet.
- A bare confirmation such as `确认提交` is blocked unless there is an active confirmation task.
- An active monthly-report task blocks ambiguous text from falling into daily report.
- A single low-risk daily or weekly effect may be marked `partial_allowed`.
- Multi-workflow writes, such as travel plus daily report, require confirmation.
- Internal Q&A is read-only at this layer and does not create write effects.
- Case progress creates an `append_case_progress` effect but does not write a case record yet.
- Legal research creates a `run_legal_research` effect but does not write daily-report content.

## Gate Decision

`build_gate_decision(plan, mode=...)` converts a `RoutingPlan` into an execution-facing `GateDecision`.

The legacy daily entry only needs to check:

- `allow_legacy_daily`.
- `block_legacy_daily`.
- `reply_text` when blocked.

In `observe_only`, `allow_legacy_daily` is always true.

In `protective_gate`, legacy daily is allowed only when the plan is owned by
daily report. Multi-effect messages are not silently downgraded into daily-only
writes.

The gate must not become a second daily-report executor. If a destructive or
editing command is already bound to an active daily-report task, the gate
delegates it to the daily workflow so that the daily workflow owns its own
confirmation chain. If the same destructive-looking text is standalone and has
no active daily context, the gate blocks it.

## Entry Points

The protective gate is wired before legacy daily execution in:

- `app/api/webhook.py`.
- `app/stream_runner.py`.

Both entry points run monthly performance collection first. If monthly collection does not handle the message, the gate decides whether the old daily-report executor may receive it.

## Why Weekly Is Added Now

Weekly report is close enough to daily report to share the reporting foundation, but different enough to prove that daily report is no longer the only fallback. This gives Agent 2.0 a small but meaningful second workflow without introducing the higher risk of case or legal research writes.

## What Is Not Done Yet

- The old executor does not consume `RoutingPlan`.
- Weekly report has no database model or renderer yet.
- Travel, case progress, and legal research are only recognized as workflow categories.
- No tool calls or database writes are executed from this plan.
- `MultiEffectExecutor` is not implemented.
- `MatterResolver` and `TravelResolver` are not implemented.

## Next Slices

1. Add persistent audit storage for `RoutingPlan` and `GateDecision`; current slice logs them.
2. Convert the worst daily-report issue-ledger cases into expected `RoutingPlan` and `GateDecision` fixtures.
3. Enable `protective_gate` for a controlled test user or window.
4. Build a weekly-report draft model and renderer after the gate proves stable.
5. Add `MultiEffectExecutor` only after gate false positives are under control.

## 2026-07-02 Historical Replay Finding, First Pass

The first 30-day production replay was run after deploying observe-only code:

- `outputs/workflow_gate_replay_webhook_30d.json`.
- `outputs/workflow_gate_replay_interaction_30d.json`.
- `outputs/workflow_gate_replay_performance_30d.json`.

Result: `protective_gate` is not ready to enable.

The main issue is not deployment safety. The main issue is that a single-message replay and the current live gate do not yet reconstruct daily-report active context. Many real legacy interactions are short contextual replies, such as:

- `确认提交`.
- `暂无问题`.
- `发我看下`.
- `我要合并`.
- `第二条到第七条是同一点`.

These are unsafe without pending context, but valid when bound to an active daily-report state. Before enabling `protective_gate`, Agent 2.0 must build active daily tasks from the current user's draft/submission state and pending interaction state.

The replay also showed that daily-report wording is broader than the first heuristic. Common valid daily phrasings include verbs such as `修订`, `更新`, `梳理`, `沟通`, `对接`, `发送`, and `打印`, not only `完成` or `处理`.

Next required slice before any protective rollout:

1. Add live daily active-task reconstruction before the gate.
2. Add historical replay context reconstruction for `report_interaction_events`.
3. Expand daily-report signal heuristics using real production phrases, grouped by behavior rather than one-off text.
4. Rerun 30-day replay and require a much smaller `review_needed` set before gray release.

## 2026-07-02 Context-Gate Slice

Implemented after the first replay:

- Added live daily active-task reconstruction from recent `DailyReport` state.
- Added historical replay context reconstruction from `report_interaction_events`.
- Added sentence-level segment classification for multi-intent messages.
- Added active-daily short-reply ownership: when a user has an active daily
  task, short non-question and non-small-talk replies can belong to the daily
  workflow; real questions and small talk are still kept out.
- Added daily context effects:
  - `confirm_daily_report`.
  - `legacy_daily_context_action`.
- Kept the legacy daily executor unchanged.
- Kept production mode as `observe_only`; `protective_gate` is still not enabled.
- Changed the gate boundary: daily-owned edit/delete/clear commands with active daily context are delegated to the daily workflow instead of being handled by the gate.

Verification on server:

- Agent2 key tests: `50 passed`.
- Product gate: `137 passed`.
- Online smoke: `7/7 passed`.

30-day replay after this slice:

- `interaction`: total `750`, allow `727`, block `23`, write-review `13`.
- `webhook`: total `1359`, allow `150`, block `1209`, unknown-blocked `1031`.
- `performance`: total `23`, allow `0`, block `23`, review-needed `0`.

Interpretation:

- The meaningful daily workflow replay metric is `interaction`, because it has
  per-message snapshots and backend actions. It improved from the first pass
  `review_needed=644` to layered `write_review=13`.
- The raw `webhook` table is not a rollout metric yet because it lacks the
  daily before/after snapshots needed to reconstruct contextual replies.
- The remaining `interaction` write-review set is mostly true questions, monthly
  templates that should not silently fall into daily report, missing-context
  undo/edit text, and older test/probe cases.

Next required slice before any gray release:

1. Split replay metrics into legacy write-impact vs legacy non-write response so
   non-report chatter does not inflate false-positive counts.
2. Add a production audit table for `RoutingPlan` and `GateDecision`.
3. Add fixtures from the remaining `interaction` review set.
4. Only then test `protective_gate` for a single test user or short time window.
