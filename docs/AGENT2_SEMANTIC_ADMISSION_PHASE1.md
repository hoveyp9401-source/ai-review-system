# Agent2 cross-domain Semantic Admission Phase 1

Status: implementation and Shadow acceptance document. Runtime results, deployed hashes, PostgreSQL evidence, and final verdict are recorded separately and must not be inferred from this architecture description.

## Production seam

```text
Webhook / Stream / Manual adapter
  -> Agent2TurnRuntime(VerifiedTurnRequest)
  -> trusted tenant, actor, conversation, source message, permissions and versions
  -> one LLM Semantic Proposal
  -> deterministic DomainAdmissionEngine
       -> one Decision per exact segment/action
       -> Ticket for each admitted mutation
       -> InformationPending for missing required facts
       -> no authority for blocked/review/deferred decisions
  -> admitted SemanticInterpretation only (Enforce)
  -> typed Planner
  -> domain Compiler
  -> Executor revalidation + authoritative Ticket row lock
  -> business write + committed receipt + Ticket consumption in one transaction
  -> OperationOutcome
  -> receipt-driven state settlement
  -> Outcome-only Reply Composer
```

The LLM proposes facts and evidence spans. It does not select database IDs, authorize a mutation, decide that a transaction committed, decide a transport state, or compose an unverified success claim.

There is no fallback from failed Enforce Admission to an older write path. Disabled mode keeps the recorded pre-rollout behavior; Shadow persists non-executable audit artifacts while the existing behavior continues; Enforce makes the admitted interpretation and authoritative Ticket mandatory.

## Trust boundary

Admission takes identity and resources only from server-resolved inputs:

- tenant, actor, conversation, turn and source-message identity;
- current conversation-state version;
- current report IDs, owner, date, status, version and stable item IDs;
- permission-filtered visible cases and their versions;
- permission-filtered recent CaseProgress objects and versions;
- active travel-collaboration candidate, participant set, expiry and version;
- active case-follow-up policy/task objects and versions;
- explicit read-access capabilities.

Natural-language text cannot supply or override those resources. Every action must bind exactly one ordered source substring. The trusted code verifies source offsets and SHA-256. Facts and objects cannot borrow grounding from another segment.

## Decision and execution matrix

| Domain | Cognitive operation | Admission requirement | Execution authority |
|---|---|---|---|
| Report | query daily/weekly/monthly | trusted readable report scope | read-only; no Ticket |
| Report | append | explicit report action or one compatible active report task; unique writable report/date/section/version | one report Ticket |
| Report | edit/delete/merge | stable item IDs from the trusted snapshot; exact replacement where applicable; writable expected version | one report Ticket |
| Report | clear section/report | unique target and explicit high-impact operation; confirmation policy remains separate | one report Ticket |
| Report | submit/reopen/copy | exact report/status/version and operation-specific legality | one report Ticket |
| Case | answer/query progress | permission-scoped visible case/read resource and grounded query | read-only; no Ticket |
| Case | create progress | one authorized case plus an asserted, exact-segment fact/action/status/plan/readiness/blocker | one case Ticket |
| Case | update/delete progress | one permission-scoped stable progress ID and version; grounded patch or reason | one case Ticket |
| Case | link progress | stable progress/version plus trusted allowlisted related-object IDs; absent document authority blocks | one case Ticket |
| Case | update follow-up policy | one authorized case, current case/policy versions, closed policy fields | one case Ticket |
| Case | trigger follow-up now | one authorized case and current policy/task legality | one case Ticket |
| Travel | register intent | explicit personal, non-negated, non-hypothetical trip; grounded unique destination and parseable date | one travel Ticket |
| Travel | answer collaboration | one active same-scope candidate, current participant, unexpired status/version, canonical answer | one travel Ticket |
| Runtime | query last operation status | explicit read capability and grounded supported domain | read-only; no Ticket |
| Knowledge | enterprise search | explicit read capability and grounded query | read-only; no Ticket |
| Unknown | any operation | no contract | blocked, zero Ticket, zero write |

All mutation actions fail closed if the authoritative Ticket is missing, inactive, expired, reused, cross-scope, source-drifted, claim-drifted, object-version-drifted, state-version-drifted, permission-revoked, or unknown to the deployed contract/policy version.

## Report rules

- Daily, weekly and monthly use one upper Report command boundary; the existing typed-daily adapter may remain below it.
- “没其他风险” while answering the active report risk prompt is `no_op`: it is not stored as content and cannot become CaseProgress.
- A report mutation cannot target an old report merely because it is recent. Date, ID, owner, status and version must match trusted resources.
- Item mutation requires stable IDs. “上一条”“最后一条” or model list order is not database authority.
- Multiple report mutations in one turn receive sequential expected versions derived from the same trusted base snapshot.
- Report queries are admitted read-only without a Ticket; a query cannot be upgraded to a write downstream.
- A Case write may create a report projection only after its committed receipt and the independent projection policy. Direct daily write plus derived projection for the same fact is prohibited.

## Case rules

- Generic words such as “案件材料” are not case identity.
- A case alias, number, external number or full name must resolve to exactly one case inside the current permission set and be grounded in the action segment.
- Questions, quotations, examples, hypotheticals, negations and product explanations cannot authorize CaseProgress.
- Stored business facts preserve the user's evidence and cannot be semantically strengthened by the reply layer.
- Update, delete and link operations require stable progress identity and optimistic version. Missing trusted related-document resources makes document linking unavailable in Enforce rather than guessed.
- Follow-up policy operations remain inside the existing Case domain; this phase does not turn on proactive sending or automatic report projection.

## Travel rules

- Registration requires the current user to assert a real trip, a unique grounded destination and a parseable date in the configured timezone.
- Missing date creates InformationPending with zero Ticket and zero write.
- Collaboration answers require exactly one active candidate in the same tenant/user/conversation context. Candidate ID, participant, expiry and version are revalidated.
- Notification state is not inferred by Admission. Replies may say provider-accepted only from the transport receipt; that state is not delivery confirmation or user agreement.

## Composition

Admission evaluates each exact segment/action independently:

- one ambiguous or invalid sibling blocks only itself;
- every admitted mutation gets its own Ticket and receipt;
- Case and Travel are primary domain writes;
- Report is either an explicit independent action or a later receipt-derived projection;
- execution order and optimistic versions are deterministic;
- partial success produces one Outcome per action;
- state advances only after all planned executable actions have a committed or stable duplicate receipt; Admission-only non-write blocks do not masquerade as failed writes;
- Reply composition evaluates success language per Outcome, so one successful sibling cannot lend “已记录” to a failed sibling.

## Information continuation

InformationPending is not a delayed command. It records the original scope, object/version, missing fields, source digest, acceptable answer form, expected state version and TTL with `business_write_allowed=false`.

A later short answer can continue only when:

1. exactly one active Pending matches tenant, user and conversation;
2. source replay, expiry, state drift, permission drift and object drift checks pass;
3. the new message explicitly and exactly fills the missing field;
4. trusted code combines the original protected facts with only that new field;
5. the combined proposal re-enters a formal fresh Admission cycle;
6. the new Ticket binds the original Pending ID, current source message, new evidence span and final authorized command;
7. the Pending is consumed only after the new business receipt commits.

Multiple Pending, “顺便” unrelated content, arbitrary later chat, cron, review artifacts and deferred artifacts cannot continue a write. Failure leaves an audit result and zero business mutations.

## Review and deferred artifacts

`SemanticReviewItem` and `DeferredSemanticEvent` are audit-only. Their candidate payload is an exact digest-only three-key object (`action_id`, closed-set `decision_verdict`, and stable digest/reference-only `evidence_refs`) and is bound back to the persisted Admission Decision. Raw user text and arbitrary nested payloads are rejected. They always carry:

```text
audit_only = true
business_write_allowed = false
requires_fresh_admission = true   # deferred events
```

No worker, timer, Planner or Executor reads them as write authority. They do not create an Outcome with `actual_write=true`, and the user reply must not describe them as recorded business facts.

## Flags and rollout

All controls default off:

```text
agent2_semantic_admission_enabled
agent2_semantic_admission_enforce
agent2_semantic_admission_review_capture
agent2_semantic_admission_deferred_capture
agent2_semantic_admission_shadow_replay
agent2_semantic_admission_tenant_allowlist
agent2_semantic_admission_user_allowlist
```

Rollout order is migration -> local and PostgreSQL integration -> sealed replay/adversarial tests -> Shadow with cancelled Tickets/Pendings -> read-only structural metrics -> limited Enforce only if every gate passes. The tenant boundary remains `sandbox-agent2-phase2-20260711`; the user boundary reuses the existing Pang Hao/Liu Cong internal IDs. Case-follow-up send and automatic report projection remain disabled.

## Structural circuit breaker

The digest-only safety evaluator reports and recommends disabling Enforce when any of the following is non-zero:

- admitted mutation without exactly one persisted Ticket;
- read-only Decision with a Ticket;
- cross-scope or orphan Trace/Decision/Ticket/Pending;
- Ticket/Decision operation or linkage mismatch;
- Shadow Ticket not cancelled;
- consumed Ticket without a receipt reference;
- expired Ticket still issued;
- Pending able to authorize writes;
- expired active Pending or consumed Pending without its consuming Trace;
- unknown mode, status or verdict.

A zero structural count is necessary but not sufficient for rollout. It does not replace semantic replay, usability, PostgreSQL, runtime parity, receipt/audit correlation, deployment hashes or real-user evidence.
