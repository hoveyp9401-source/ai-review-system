# Agent2 Semantic Admission Phase 1 — Open Issues

This ledger records capability blockers separately from implementation defects. A
feature that is deliberately kept behind a disabled kill switch is not counted as
production-ready merely because its local happy-path tests pass.

## SA-P1-001: Case-progress document links have no trusted document resource

- Status: open / fail-closed
- Severity: P1 usability, not a write-safety bypass
- Scope: `link_case_progress`
- Evidence: the verified Runtime currently supplies permission-filtered Party IDs and TravelIntent IDs, but there is no authoritative document registry/resource scoped to tenant, user, conversation, and allowed Case.
- Current behavior: any proposed `related_document_ids` value that is absent from `turn.resources.visible_document_ids` is blocked with `case_progress_link_target_not_trusted`; no Admission Ticket is issued and writes remain zero.
- Regression: `test_case_progress_delete_and_link_require_grounded_or_trusted_change_fields`.
- Resolution gate: introduce a PostgreSQL-backed, permission-filtered document resource with stable ID and version, then add Admission and executor live-version checks. Do not accept model-proposed document IDs directly.

## SA-P1-002: Automatic Case-to-Report projection lacks durable source-receipt authority

- Status: open / feature hard-disabled
- Severity: P0 if automatic projection is enabled; rollout blocker while disabled
- Scope: `derived_committed_receipt`, Case Follow-up to Report projection
- Evidence: the current projection sequence commits the Case write before creating the independent projection request. A process failure in that interval can lose the projection request, and the generic authority label does not itself prove a committed source receipt from PostgreSQL.
- Current mitigation: `case_followup_report_projection_enabled=false`; the Phase 1 rollout must preserve this value and prove that the disabled branch opens no session, creates no request, and writes no Report item.
- Resolution gate: add a durable committed-receipt outbox (or equivalent same-transaction handoff), lock and verify the source receipt before projection execution, and replay crash/retry cases without duplicate Report items.

## SA-P1-003: Legacy compatibility authority has a permission-revocation race window

- Status: open / transitional authority only
- Severity: P1 while Enforce is off; P0 if relied on for high-impact or expanded Canary writes
- Scope: `legacy_user_compatibility`
- Evidence: the authoritative Semantic Ticket path revalidates and locks the live identity binding inside the business transaction. The compatibility path preserves the existing runtime but does not yet provide the same identity-row lock boundary for a concurrent permission revocation.
- Current mitigation: compatibility is not represented as admin authority, cannot consume a Semantic Ticket, and must not be used as evidence for Enforced writes. Canary and tenant allowlists remain unchanged; follow-up sending and automatic projection stay disabled.
- Resolution gate: either move all current user-originated mutations to `semantic_ticket`, or add equivalent same-transaction live identity and permission fencing to the compatibility path before expanding its authority.

## SA-P1-004: Duplicate receipts need exact ingress scope and live authorization

- Status: implementation resolved / server Enforce validation pending
- Severity: P1 confidentiality and idempotency correctness
- Scope: Business, periodic Report, and typed-daily duplicate replay
- Remediation: Business, periodic Report, and typed-daily replay now validate the consumed Admission Ticket and the exact trusted execution scope before returning a duplicate receipt. Non-terminal receipts, claim drift, cross-actor/scope replay, and idempotency collisions fail closed without exposing the prior business snapshot.
- Local evidence: `test_agent2_business_sql_executor.py`, `test_agent2_sql_admission_ticket_store.py`, `test_agent2_report_sql_admission_store.py`, and `test_agent2_typed_daily_executor_v3.py`; the final Agent2 suite reports `1596 passed, 1 skipped`.
- Remaining rollout gate: because the production rollout in this phase remains Shadow-only, no claim is made that a production Enforce replay has been exercised. Enforce remains disabled until a later controlled Canary validates the same path against production PostgreSQL.

## SA-P1-005: Selection Pending Enforce continuation requires fresh Admission

- Status: implementation resolved / production Enforce disabled
- Severity: P0 write-safety
- Scope: ambiguous Case selection and short replies such as “第二个”
- Remediation: ambiguous Admission now produces an audit-only `TrustedSelectionRequest` with zero Ticket. Conversation State persists a scoped Selection Pending with CAS. An exact later answer revalidates scope, TTL, state, permission, object/version, the protected original-source digest, and current selection evidence before a fresh Admission may issue a Ticket. Missing or conflicting authority remains zero-write.
- Local evidence: `test_agent2_selection_pending_fresh_admission.py`, `test_agent2_selection_pending_runtime_integration.py`, `test_agent2_selection_runtime_atomicity_review.py`, and the Webhook/Stream/Manual entrypoint review suites cover state CAS, duplicate ingress, cross-user/tenant/conversation, expiry, object/version drift, audit atomicity, and entrypoint parity.
- Remaining rollout gate: legacy pre-runtime Selection paths still return before the unified Runtime in Shadow mode, so Shadow trace coverage is incomplete for those already-handled messages. Enforce bypass is closed, but Shadow evidence must not be used to advance Enforce until the pre-runtime observation gap is removed or separately instrumented.

## SA-P1-006: Follow-up selection metadata is not yet authoritative enough

- Status: open / fail-closed in Enforce
- Severity: P1 usability, P0 if enabled without correction
- Scope: Follow-up policy selection
- Evidence: existing candidate metadata can bind a Case version where a policy version is required, and does not freeze every Pending/Task/Policy authority field needed for a safe policy mutation.
- Current behavior: follow-up selection continuation must not receive a fresh Ticket in Enforce until the dedicated authority snapshot and SQL revalidation are implemented.
- Resolution gate: dedicated policy/follow-up Pending contract, correct stable IDs and versions, and same-transaction live validation.

## SA-P1-007: Frozen-runtime Blind replay did not produce a scoreable actual

- Status: open / Enforce gate blocked
- Severity: P1 evaluation reliability; not a deployed Shadow write-safety defect
- Scope: 26-case sealed Semantic Admission Blind runner
- Evidence: the single post-deployment run stopped when the model proposed `edit_daily_item` without the mandatory `daily_item_target` binding. `SemanticInterpretation.from_payload` correctly failed closed with `ValueError`; no actual artifact was published and sealed labels were never made available to the runner.
- Non-cherry-pick decision: the unchanged nondeterministic run was not retried. The failed attempt is preserved in `evals/agent2/semantic_admission/actual_after_fix_failure.json` with `acceptance_eligible=false`.
- Consequence: this phase cannot advance to Enforce or `READY_FOR_TWO_USER_CANARY`. Shadow may remain enabled because the failure occurred in offline evaluation and the production Enforce switch is false.
- Resolution gate: make the Blind runner publish a deterministic per-case contract-failure artifact without weakening the production contract, freeze a new evaluator hash, and run a new declared evaluation cycle with separately sealed scoring and independent human adjudication.
