# ADR 0019: Segment-scoped Domain Admission and composition

Status: accepted for Agent2 Semantic Admission Phase 1.

## Decision

Agent2 keeps one semantic proposal seam. Trusted turn context and permission-scoped resources are presented to the existing Semantic Interpreter, and every proposed action is evaluated independently by deterministic Domain Admission before any Planner or Executor may consume it. This replaces scattered write-routing heuristics; it is not a fourth router and not a second production model vote.

The public audit/interchange interface is the flat `agent2.domain_admission.v1` envelope. `trace`, `decision`, and `ticket` repeat the verified `tenant_id`, `user_id`, `conversation_id`, `source_turn_id`, and `source_message_id` rather than nesting `scope` or `source`. This deliberate duplication gives the wire contract a direct, reviewable mapping to the PostgreSQL columns and makes tenant fencing explicit on every row. The sole structured target field is `object_ref`; the persistence adapter maps it atomically to `object_type`, `object_stable_id`, `object_version`, and `object_label`.

All Admission-owned identifiers (`trace_id`, `decision_id`, `ticket_id`, `pending_id`, `review_id`, and `deferred_event_id`) are deterministic UUIDv5 values created by trusted code from canonical JSON claims. External tenant, user, conversation, turn, message, action, segment, object, receipt, and idempotency identifiers retain their native bounded string formats. Model-proposed identifiers never become database identities.

## Trace and Decision

An `AdmissionDecision` is the fail-closed verdict for one proposed `(verified scope, source, segment span, domain, operation, object, expected versions)` tuple. Its closed verdict set is:

```text
admitted
blocked
no_op
information_required
review_only
deferred_audit_only
```

Each decision belongs to one immutable `AdmissionTrace`. The trace records the verified scope and source, expected conversation-state version, canonical proposal SHA-256, contract and policy versions, trace status, summary, stable idempotency key, and creation time. Raw proposal text is not part of the Trace contract and must not be persisted in the Trace table. Optional operational metadata must be separately allowlisted and demonstrably free of message, segment, case, report, or travel正文.

Decision artifact rules are closed:

- `admitted` mutations require a complete `object_ref`, exactly one `ticket_id`, and no `pending_id`;
- admitted read-only operations require no `ticket_id` and no `pending_id`; the closed read-only allowlist is `query_daily_report`, `query_periodic_report`, `answer_case_query`, `query_case_progress`, `query_operation_status`, and `search_enterprise_knowledge`;
- `information_required` requires exactly one `pending_id` and no `ticket_id`; its `object_ref` may be null when the missing information is the target itself;
- `blocked`, `no_op`, `review_only`, and `deferred_audit_only` have neither Ticket nor Pending;
- a non-null `object_ref` is always complete: `object_type`, `stable_id`, and nullable non-negative `version`, with an optional label;
- unknown domains, operations, verdicts, artifact types, or incomplete object references are rejected.

`blocked` and `no_op` decisions must remain persistable even when no stable object can be resolved, so their object columns are nullable as an all-or-none group. A Decision is semantic evidence only: it is not an `OperationOutcome`, business receipt, or proof of a committed write.

## Exact segment grounding

Every Decision and Ticket binds the exact verified segment by:

```text
segment_id
segment_text_sha256
segment_start_offset
segment_end_offset
```

The offsets use the canonical source-turn text coordinate system. Domain Admission verifies that the source slice equals the proposed segment and that its SHA-256 matches. Each action must bind exactly one segment; its entities and intent must belong to that segment. A model-proposed object or fact cannot borrow evidence from a sibling segment. The admitted Planner input is projected only from admitted actions and their grounded segments/entities/intents; blocked proposal material remains audit-only.

## Admission Ticket

Only an admitted mutation Decision may issue an `AdmissionTicket`. An admitted read-only Decision never receives an execution Ticket and therefore cannot be replayed as write authority. A Ticket is short-lived, deterministic, idempotent, and bound to:

- the exact verified tenant, user, conversation, turn, and message;
- the Decision, action, segment span and hash;
- the domain, operation, stable object identity and expected object version;
- the expected conversation-state version and Admission policy version;
- `authority_scope`, the exact `allowed_changed_fields`, `fact_claims_sha256`, and `authorized_command_sha256`;
- the contract version, issue time, expiry, and TTL.

`authority_scope` contains the exact domain facts from which trusted code compiled the authorized command. It is persisted as protected business evidence, not emitted to logs or user replies. `fact_claims_sha256` and `authorized_command_sha256` are calculated from canonical JSON. A Planner may carry a Ticket reference, but an Executor must load and lock the authoritative Ticket row; an unpersisted or forged dictionary has no authority.

A Ticket authorizes an Executor only to attempt its named operation. It never replaces tenant/user/conversation fencing, live permission and existence checks, optimistic object/state version checks, idempotency, a database transaction, receipt, or audit. The Executor revalidates every claim and atomically consumes the Ticket in the same transaction as the business write and committed receipt. Expiry, scope drift, source drift, span/hash drift, command-claim drift, object/state-version drift, permission loss, unknown policy/contract, invalid status, or Ticket reuse fails closed with zero business writes.

Ticket status is closed to:

```text
issued
consumed
expired
cancelled
conflicted
permission_revoked
```

Only `consumed` may contain `consumed_at` and `consumed_receipt_ref`; both are required together. Every other status must have both fields null. `executor_revalidation_required` is always true and `proves_business_write` is always false. Only the committed receipt and resulting `OperationOutcome` may state that a write succeeded.

## Pending, review, and deferred artifacts

Missing information creates an `InformationPending`, not a guessed target and not a write. It is fenced to the same flat tenant/user/conversation/source fields, exact segment, operation, expected state/object versions, acceptable-answer contract, and TTL. A reply re-enters semantic interpretation and Domain Admission as a new turn; the Pending itself is never executable. Ambiguous selection and approval remain the separate `SelectionPending` and `ConfirmationPending` protocols.

`SemanticReviewItem` and `DeferredSemanticEvent` are audit-only artifacts. Review items support Shadow comparison and human adjudication. Deferred events preserve a possible future semantic signal without scheduling or authorizing business work. Neither artifact may be consumed by a business Executor or promoted into a write by a timer or worker. Any later business action requires a fresh authorized turn, Decision, Ticket, and normal Executor validation.

## Persistence and transaction rules

The JSON Schema is the canonical wire shape; PostgreSQL is its normalized flat persistence mapping. Structured JSON fields are limited to `authority_scope`, `allowed_changed_fields`, evidence references, Pending answer contracts, and audit-only candidate/payload snapshots. The Trace table stores only `proposal_sha256`, never raw proposal text. The persistence adapter must prove a lossless round trip between `object_ref` and the four object columns.

Trace is inserted first. An admitted mutation Decision and its Ticket, an admitted read-only Decision without a Ticket, or an information-required Decision and its Pending are inserted in one transaction. The Decision-to-artifact foreign keys are deferrable to support artifact cycles, while artifact-to-Decision foreign keys remain immediate. No admitted mutation Decision may commit with a missing Ticket, no admitted read-only Decision may commit with a Ticket, and no information-required Decision may commit with a missing Pending.

## Rollout and rollback consequences

- Production uses one model proposal followed by deterministic admission; a second model may score sealed replay or Shadow artifacts but has no write authority.
- New controls default off, run in Shadow first, and remain fenced to the existing Sandbox tenant and the Pang Hao/Liu Cong Canary allowlist.
- Turning Admission off never means allow-all. Rollback must first route to a known-good runtime or block writes, revoke/expire outstanding Tickets, and preserve audit evidence.
- Source spans, Ticket claims, receipt linkage, and PostgreSQL round trips are deterministic test surfaces.
- Unknown domain, operation, state, policy, contract, or artifact type blocks. There is no “recent object” or “last record” fallback.

## Rejected alternatives

- **A fourth intent router:** competing authorities make failures harder to reproduce.
- **Two-model production voting:** disagreement still needs deterministic policy and adds latency without transaction evidence.
- **Nested scope/source wire objects:** they add an unnecessary mapping seam and obscure row-level fencing.
- **LLM-selected database IDs:** this crosses the authorization seam.
- **Self-attested command dictionaries:** they can be forged or replayed without an authoritative Ticket row.
- **Executable deferred events:** delayed context and permission drift require a fresh Admission cycle.
