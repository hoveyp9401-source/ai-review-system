# Department Collaboration Bot Workflow Spec

Status: draft for grilling.

## Goal

Turn the current legal daily-report bot into a department collaboration assistant. It should help the department collect information, route follow-ups, integrate repeated reporting work, and prepare clear summaries for managers and leaders.

Daily report is no longer the whole product. It is the first mature workflow inside a broader assistant.

## Users

- Department operator: configures collection tasks, checks exceptions, tests new workflows, and approves leader-facing output.
- Team owner: receives metric or task prompts and replies in batches or gradually.
- Department member: submits daily reports, travel plans, case progress, and follow-up details.
- Case handler: replies to case progress collection prompts.
- Leader: receives filtered monthly summaries, risks, coordination items, and decision requests.

## Capability Map

### 1. Daily Report

Purpose: keep the current daily-report workflow stable and smarter.

Required behavior:

- Normal submission.
- Gradual submission.
- Modification.
- Recall.
- Late submission.
- Merge with existing daily draft.
- Query history.
- Copy yesterday or recent items.
- Understand yesterday's plans, unfinished items, or risk items and supplement today's content.
- Avoid mixing daily-report content with monthly-report/performance replies.

Key data objects:

- DailyReportDraft.
- DailyReportSubmission.
- DailyReportHistory.
- DailyReportIntent.
- DailyReportOperationLedger.

### 2. Monthly Report

Purpose: collect team metric explanations and plans, then assemble team and department monthly reports.

Required behavior:

- Accept source metric data provided by the operator.
- Send each team owner a metric overview first.
- Then send a fill-in prompt for unfinished reason/existing problem, next-month target, and action plan.
- Support one-shot replies, per-metric replies, partial replies, later supplements, and natural-language edits.
- Archive replies by metric.
- Generate each team's monthly report for confirmation.
- After all required team reports are collected, generate a department monthly report for operator review before sending to the leader.

Key data objects:

- DepartmentMonthlyReport.
- TeamMonthlyReport.
- MetricItem.
- MetricReplyState.
- MonthlyReportCollectionTask.
- LeadershipMonthlyReport.

### 3. Travel Collaboration

Purpose: identify duplicated or overlapping travel work and prompt coordination.

Inputs:

- Daily-report travel mentions.
- Operator-provided travel spreadsheet.
- Future: approval/attendance/travel systems if available.

Required behavior:

- Detect overlapping destination, date range, customer/project, or task.
- Privately notify relevant people.
- Phrase suggestions as optional coordination, not mandatory assignment.
- Track whether the overlap was ignored, accepted, or resolved.

Key data objects:

- TravelPlan.
- TravelOverlap.
- TravelCoordinationSuggestion.

### 4. Case Progress Collection

Purpose: collect progress on legal cases from responsible handlers and consolidate case status.

Inputs:

- Source case sheet with plaintiff and defendant.
- Daily-report case mentions.
- Fixed monthly collection time.
- Case milestone triggers.

Required behavior:

- Maintain case-handler mapping.
- Ask the responsible handler for missing progress.
- Recognize progress updates from daily reports.
- Avoid duplicate asks when progress has already been submitted.
- Generate case progress summaries by case, handler, department, or risk level.

Key data objects:

- CaseRecord.
- CaseProgressItem.
- CaseProgressCollectionTask.
- CaseMilestone.
- CaseRiskSignal.

### 5. Internal Q&A

Purpose: answer common internal questions using an internal knowledge base.

Required behavior:

- Use approved internal materials as sources.
- Cite or name the source document when possible.
- Say when the knowledge base has no confident answer.
- Escalate unclear policy, compliance, or legal-risk questions instead of inventing answers.

Key data objects:

- KnowledgeSource.
- RetrievedEvidence.
- AnswerDraft.
- EscalationRequest.

### 6. Legal Research

Purpose: future research assistant capability. The exact workflow is still undecided.

Possible directions:

- Research memo drafting.
- Legal issue spotting.
- Similar case or regulation search.
- Contract/legal risk comparison.
- Litigation strategy notes.

This area should not be built until the expected output, source authority, review process, and acceptable risk are clearer.

## Cross-Workflow Architecture

### Unified Intake

Every incoming message first enters a unified intake layer. The intake layer should decide whether the user is submitting daily work, replying to monthly metrics, updating case progress, mentioning travel, asking a question, or editing an existing artifact.

The decision should use active collection tasks, sender identity, recent conversation state, message shape, and explicit keywords. It should not rely only on a fixed enum list.

### Conversation State

The bot needs durable per-user and per-task state:

- Active workflow.
- Active collection task.
- Pending confirmation.
- Last generated draft.
- Last parsed structured data.
- Message-to-artifact links.
- Edit history.

This is the layer that lets one person reply "先发1、2、3，后面补4、5、6" without confusing daily report and monthly report.

### Domain Modules

Each domain should own its parser, validator, state machine, renderer, and tests:

- daily_report.
- monthly_report.
- travel_coordination.
- case_progress.
- internal_qa.
- legal_research.

The shared platform should provide message intake, identity, storage, notification, confirmation, audit logging, and scheduling.

### Data Contracts

Each workflow should convert free-form messages into structured records before rendering output. Raw text must be preserved, but leadership-facing or official output should render from structured data, not from pasted raw fragments.

### Testing Strategy

Use the existing daily-report issue ledger as the quality bar. For each workflow:

- Unit tests for parsing.
- State-machine tests for partial, batch, edit, and confirm flows.
- Renderer tests for no truncation, no duplicate raw fragments, and clean layout.
- Online smoke tests before real sending.
- Shadow mode for new workflows.

## First MVP Proposal

Phase 1 should focus on two tracks:

1. Daily report v2 foundation: keep all existing behavior stable while introducing unified intake, clearer workflow state, and better test fixtures.
2. Monthly report collection and aggregation: keep it in test/shadow mode first, then send to real team owners only after operator approval.

Travel collaboration, case progress, Q&A, and legal research should get data-model sketches and intake hooks, but not full automation in the first phase.

Reason: daily report is already production-critical, and monthly report is the next urgent business workflow. Building all six areas at once would blur responsibility and make the bot harder to trust.

Decision: the first phase can prioritize foundation work even if it does not immediately add visible new user-facing features. The accepted strategy is to keep the existing daily-report experience stable while adding the new intake/router/session layer in observe-only or narrowly gated mode.

## Phase 1 Execution Plan

The first phase should be built as an evolutionary refactor, not a rewrite.

Parallel safety track: daily report gets its own stabilization program. The workflow router may run observe-only while daily report is still producing daily P0/P1 issues. New monthly-report traffic should not take over real users until the daily-report quality gate is green.

### Step 1: Freeze the current daily-report behavior

Create a behavior ledger from the existing production cases and tests. The ledger should list every supported daily-report operation and the historical bug cases that must never regress.

Done means:

- All current daily-report smoke tests pass.
- Known issue-ledger cases pass.
- Each supported operation has at least one fixture.
- The system can tell whether a new change broke daily report behavior.

### Step 2: Introduce a unified intake envelope

Wrap every incoming DingTalk message in a shared envelope before it reaches a workflow.

The envelope should contain:

- sender identity.
- chat or conversation id.
- timestamp.
- raw text.
- attachments if any.
- active task candidates.
- recent conversation state.
- selected workflow intent.

This step should not change user-facing behavior. It only gives later workflows a reliable entry point.

Initial implementation note: `app/workflows/intake.py` now defines the observe-only envelope/router. `app/api/webhook.py` records route observations without changing the existing execution order.

### Step 3: Split workflow routing from workflow execution

Routing should decide which workflow owns the message. Execution should only handle domain behavior after ownership is clear.

Initial workflow routes:

- daily_report.
- monthly_report.
- unknown_or_help.

Later routes:

- travel_coordination.
- case_progress.
- internal_qa.
- legal_research.

Done means:

- Daily-report messages still enter daily_report.
- Monthly-report replies no longer get written into daily reports.
- Ambiguous messages are held for clarification or routed by active task state.

### Step 4: Build monthly report in shadow mode

Monthly report should first run without sending real leadership output automatically.

Done means:

- Operator can create a monthly collection task.
- The bot can send metric overview and fill-in prompt to selected test users.
- Replies can be partial or complete.
- Parsed results are stored as MetricItem and TeamMonthlyReport.
- The generated team report can be confirmed and edited.
- The generated department report is sent only to the operator for review.

### Step 5: Add leadership renderer only after structured data is correct

Leadership output must render from structured monthly data, not from raw reply fragments.

Done means:

- No raw fragment dumping.
- No duplicate concatenation.
- No truncation marks.
- Clear status/risk judgment.
- Important issues are filtered.
- Detailed appendices are separate from the leader summary.

### Step 6: Prepare expansion hooks

After daily_report and monthly_report are stable, add empty-but-real contracts for:

- travel plan.
- case progress.
- knowledge Q&A.
- legal research request.

This keeps the architecture open without pretending those workflows are production-ready.

## Working Specs

- `workflows/daily-report-v2.md`: the production-stability and intake-refactor track.
- `workflows/daily-report-stability-program.md`: the daily triage, ledger, smoke, and release-gate track.
- `workflows/monthly-report-v1.md`: the metric collection and monthly aggregation track.

## Open Questions

1. Should the bot's public identity be renamed from "法务日志机器人" to a broader department assistant name?
2. Which two workflows must be production-ready first?
3. Who can approve messages before they are sent to leaders or team owners?
4. Which data sources are authoritative for monthly metrics, travel plans, and case records?
5. What content is allowed to be answered by Q&A without human review?
6. What legal research outputs are useful enough to build, and who reviews them?
