# Monthly Report V1 Workflow Spec

Status: draft.

## Purpose

Collect monthly metric explanations and plans from team owners, assemble team monthly reports, and generate a filtered department monthly report for leadership review.

This workflow should start in shadow/test mode and only send real leadership output after operator approval.

## Scope

Monthly report v1 covers:

- Receiving operator-provided metric data.
- Sending metric overview to each responsible team owner.
- Sending a fill-in prompt for required reply fields.
- Supporting batch replies, partial replies, one-metric-at-a-time replies, and later supplements.
- Supporting natural-language edits after draft generation.
- Generating team monthly reports.
- Generating a department monthly report after all required teams are collected.

Monthly report v1 does not yet cover:

- Automatically reading every possible source system.
- Sending final output to leadership without operator approval.
- Replacing formal performance systems.

## Trigger

The workflow can be triggered by:

- Operator creating a monthly collection task.
- Operator uploading or providing metric source data.
- A team owner replying to an active monthly prompt.
- Operator asking to regenerate, edit, preview, or finalize reports.

## Required Teams

Initial department-level collection set:

- 法务一部.
- 法务二部.
- 法务三部.
- 法务四部.
- 法务五部.
- 法务六部.
- 综合管理部.
- 朱佳佳.

The required set should be configurable per month.

## Message Flow

### Step 1: Operator creates task

Operator provides:

- month.
- required teams.
- leader mapping.
- metric data source.
- test or real mode.

### Step 2: Bot sends metric overview

The overview should be sent before the reply prompt.

It should show each metric's current completion data clearly, with metric titles emphasized and numeric placeholders or values visually distinguishable.

### Step 3: Bot sends fill-in prompt

Required fields per metric:

- 未完成原因/存在问题.
- 下月目标.
- 行动方案.

Action plan can be one line or multiple numbered lines.

### Step 4: Team owner replies

Supported reply patterns:

- Full reply for all metrics in one message.
- Partial reply for metrics 1, 2, and 3 first, then 4, 5, and 6 later.
- One metric per message.
- Natural-language correction, such as "把第3项行动方案改成..."。
- Whole-section replacement.

### Step 5: Bot updates state

The bot should reply:

- If all required metrics are complete: show the assembled team monthly report and ask for confirmation.
- If partial: tell the user which metrics are complete and which still need filling.
- If unclear: ask a narrow clarification question.

### Step 6: Operator receives department report

After all required team reports are complete, the bot generates a department monthly report for operator review.

It must not automatically send to leadership until test approval is complete.

## Data Contract

### MonthlyReportCollectionTask

- task_id.
- month.
- mode.
- required_teams.
- leader_mapping.
- metric_source_id.
- status.
- created_by.
- created_at.

### TeamMonthlyReport

- task_id.
- department.
- leader.
- month.
- metrics.
- completion_state.
- generated_report.
- confirmed_at.
- raw_reply_message_ids.

### MetricItem

- department.
- leader.
- metric_name.
- metric_type.
- unit.
- month_target.
- month_actual.
- month_completion_rate.
- year_target.
- year_actual_cumulative.
- year_completion_rate.
- yoy_change.
- mom_change.
- unfinished_reason.
- next_month_target.
- action_plan.
- raw_text.

## Validation

The parser must mark:

- Fields still containing blanks or placeholders as unfilled.
- Missing month target, month actual, or month completion rate as critical data missing.
- Amount metric completion rate mismatch when completion rate is not close to actual divided by target.
- Completion rate below 100% without unfinished reason.
- Completion rate below 100% without action plan.
- Vague action plans such as only "加强推进", "持续跟进", "加大力度", or "形成机制".
- Rate metrics without numerator and denominator as non-aggregatable.

## Aggregation Rules

- amount metrics can aggregate month target, month actual, year target, and cumulative actual by metric name.
- amount completion rate equals total actual divided by total target.
- rate metrics cannot be summed unless numerator and denominator are available.
- score metrics can show simple average and must label it as simple average.
- count metrics can be summed.
- Annual time progress for June 2026 is 50%.

## Status Rules

- Green: month completion rate is at least 100%, and cumulative completion rate is at least annual time progress.
- Yellow: month completion rate is 80% to 100%, or cumulative completion rate is slightly behind annual time progress.
- Red: month completion rate is below 80%, or cumulative completion rate is clearly behind annual time progress.

The exact threshold for "slightly" and "clearly" behind should be configurable.

## Renderer Rules

Team owner view:

- Clear metric overview.
- Separate fill-in prompt.
- Show partial completion state.
- Show assembled team report after completion.
- Support edits before final confirmation.

Operator/leader view:

- Start with the most important conclusion.
- Use short sections and readable text blocks instead of dense tables when DingTalk table rendering is poor.
- Use very small amounts of emoji only for scanning.
- Filter details into major risks, annual progress lag, coordination needs, and key next-month actions.
- Do not paste all raw action plans.
- Do not include random-test wording in real sending mode.

## Testing Gate

Monthly report v1 must include tests for:

- Metric count matches source template.
- Field extraction.
- Partial reply state.
- Batch reply state.
- Natural-language edit.
- Amount completion-rate validation.
- Rate metric no-sum behavior.
- Red/yellow/green status.
- Leadership coordination item filtering.
- Renderer has no truncation marks.
- Renderer does not duplicate raw fragments.
- Monthly replies are not written into daily report.

## Implementation Sequence

1. Define data contracts and fixtures.
2. Parse source metric data into TeamMonthlyReport and MetricItem.
3. Build collection task state.
4. Build reply parser for full and partial replies.
5. Build edit parser.
6. Build team report renderer.
7. Build department report aggregation and renderer.
8. Run all in test mode with synthetic data.
9. Run with real metric structure but NA# values.
10. Only after operator approval, enable real collection.

