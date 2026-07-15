# Legal Operations

The Legal Operations context describes matters handled by the legal team, their procedural position, lifecycle stage, responsible lawyer, and auditable progress records.

## Language

**Case**:
A legal matter uniquely identified by its business case identity, regardless of whether it has reached formal court filing.
_Avoid_: Row, task, ticket

**Plaintiff Case**:
A Case in which the represented company is pursuing a claim or enforcing an effective legal instrument.
_Avoid_: Active case

**Defendant Case**:
A Case in which the represented company is responding as defendant, respondent, judgment debtor, or another defensive party.
_Avoid_: Passive case

**Plaintiff Lifecycle Stage**:
The mutually exclusive top-level stage of a Plaintiff Case: Intended Filing, Litigation, Enforcement, or Closed.
_Avoid_: Plaintiff status, process node

**Intended Filing**:
The Plaintiff Lifecycle Stage before formal case acceptance, covering evaluation, decision, and filing preparation.
_Avoid_: Pre-litigation catch-all

**Litigation**:
The Plaintiff Lifecycle Stage after formal acceptance and before enforcement or closure; hearing is a sub-stage, not a top-level stage.
_Avoid_: Hearing as a top-level stage, In Suit

**Enforcement**:
The Plaintiff Lifecycle Stage in which an effective instrument is being enforced.
_Avoid_: Execution status

**Defendant Lifecycle Stage**:
The mutually exclusive top-level stage of a Defendant Case: Accepted, Hearing, Adjudicated, Performance, or Closed.
_Avoid_: Defendant status, Basic Information

**Basic Case Information**:
Descriptive identity and background of a Case; it is not a lifecycle stage.
_Avoid_: Basic Information stage

**Case Assignment**:
The responsibility relationship that gives one testing lawyer permission to view and operate a Case in the grey-test environment.
_Avoid_: Case copy, fixture ownership

**Case Progress**:
An auditable, user-originated update attached to one uniquely resolved and authorized Case; it does not by itself become a formal court fact.
_Avoid_: Chat history, Case note

**Report Domain**:
The unified business domain for Daily, Weekly, and Monthly reports. A report opener changes conversation focus but is never persisted as report content. Daily uses the existing daily-report adapter; Weekly and Monthly share one periodic-report model, typed lifecycle commands, optimistic versions, stable item IDs, idempotent receipts, and full-snapshot replies after every mutation.
_Avoid_: Weekly or Monthly as Chat, one-off routing branches

**Conversation Goal Stack**:
The ordered list of suspended business goals when a user switches domains. The current goal is handled first. Prior goals are resumable context only and cannot authorize a write in another domain.
_Avoid_: One global stale goal, implicit cross-domain write permission

**Operation Outcome**:
The authoritative, receipt-backed account of what one requested business operation actually changed, exposed for state progression, audit, replay, and user reply composition.
_Avoid_: Reply text, executor message, model judgement

**Selection Pending**:
A conversation-scoped request for the same user to select one stable, versioned candidate before a bound operation can continue.
_Avoid_: Confirmation Pending, recent-object guess, list-position memory

**Confirmation Pending**:
A conversation-scoped request to approve or reject one already-identified operation and target; it never resolves ambiguity between candidates.
_Avoid_: Selection Pending, enterprise approval

**Message Acceptance**:
Evidence that an external provider accepted a message request and returned a provider identifier; it is not evidence that the recipient received or read the message.
_Avoid_: Delivered, received, agreed

**Delivery Confirmation**:
Reliable provider callback evidence that a message reached the provider-defined delivery state.
_Avoid_: Message Acceptance, send success

**Case Follow-up Policy**:
The effective, versioned rule set that determines when one assigned Case is eligible for proactive lifecycle communication.
_Avoid_: Reminder setting, Scheduler configuration

**Case Follow-up Task**:
A persistent, auditable unit of proactive Case work created from one or more eligible triggers and assigned to one authorized lawyer.
_Avoid_: Notification row, prompt, cron job

**Case Follow-up Pending**:
A tenant-, user-, conversation-, Case-, and Follow-up-bound expectation that may claim a natural-language reply to one delivered Case Follow-up Task.
_Avoid_: Selection Pending, recent Case context

**Task Ledger**:
The authoritative collection of focused, active, and suspended user tasks whose transitions are receipt-driven and versioned.
_Avoid_: Conversation stack, active prompt list

**Meaningful Case Progress**:
A committed Case fact, completed work action, next action, readiness update, lifecycle-stage change, or allowlisted lifecycle-node change that advances the Case work record.
_Avoid_: Query, page view, acknowledgement, display-only edit

**Report Projection Request**:
A durable request, derived only from a committed Case fact, asking the Report Domain to decide and execute an optional report projection.
_Avoid_: Case-report dual write, automatic Daily append

**Case Report Projection**:
The stable, versioned relationship between one Case fact and one Report item, independently correctable without changing the Case fact.
_Avoid_: Copied chat text, implicit report side effect

**Message Dispatch State**:
The transport lifecycle of a Follow-up message, separate from whether the user answered the Follow-up Task.
_Avoid_: Follow-up task status, user response status
