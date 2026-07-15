# Agent2 Semantic Admission rollback

This runbook rolls back Domain Admission and Composition without deleting semantic audit evidence. It applies only to the current Agent2 Sandbox tenant and the existing Pang Hao / Liu Cong Canary boundary. It does not authorize widening that boundary.

## Safety invariant

Disabling Domain Admission must never create an allow-all path. Before enforcement is disabled, the affected users must be atomically routed to the recorded known-good runtime or all Agent2 business mutations must be blocked fail-closed. Never leave the new Planner/Executor write path active while bypassing Ticket validation.

An Admission Ticket is authorization only to attempt one exact operation. It is not a business receipt and `proves_business_write` is always false. `SemanticReviewItem` and `DeferredSemanticEvent` are audit-only. No rollback step may replay either artifact into a business Executor.

## Canonical flags

The phase exposes independently reversible controls, all defaulting off outside the recorded Canary:

```text
agent2_semantic_admission_enabled
agent2_semantic_admission_enforce
agent2_semantic_admission_review_capture
agent2_semantic_admission_deferred_capture
```

The first flag evaluates Domain Admission. The second requires a valid authoritative Ticket for mutations. Review and Deferred capture are audit-only and never grant write authority. Existing tenant and user allowlists remain mandatory and are not changed by this runbook.

## Immediate rollback

1. Record the incident time, deployed runtime hash, previous known-good runtime hash, Admission policy version, tenant, Canary users, affected source message IDs, and current flag/route versions.
2. Atomically do one of the following before disabling enforcement:
   - route both Canary users to the recorded known-good Agent2 runtime;
   - route both Canary users to Agent1;
   - activate the mutation kill switch so Agent2 remains readable but every business write fails closed.
3. In the same versioned configuration change, set `agent2_semantic_admission_enforce=false` and then `agent2_semantic_admission_enabled=false`. Neither false value means “admit without a Ticket.”
4. Disable Review or Deferred capture only if audit capture contributes to the incident. Leaving capture enabled is permitted only when it cannot affect routing, replies, Planner input, or business writes.
5. Reload only the affected API/Stream workers using the existing deployment model. Do not expand the Canary.
6. Invalidate outstanding `issued` Tickets through the typed operational invalidation path, recording reason, actor, cutoff, receipt, and audit. If that path is unavailable, keep writes blocked and wait at least the maximum Ticket TTL. Merely changing a flag is not evidence that an already-issued Ticket was invalidated.
7. Executors must reject any Ticket whose contract or policy version is not accepted by the active runtime, whose authoritative status is not `issued`, or whose issue time predates the recorded rollback cutoff when the rollout epoch requires invalidation.
8. Verify that no post-cutoff business receipt references a pre-cutoff, expired, cross-scope, conflicted, revoked, cancelled, or previously consumed Ticket.

## Read-only verification

Run these queries using the normal read-only operational role. Replace placeholders locally; do not copy production identifiers or protected `authority_scope_json` content into tickets or reports.

Ticket lifecycle and consumption consistency:

```sql
SELECT ticket_status, count(*)
FROM agent2_semantic_admission_tickets
WHERE tenant_id = :tenant_id
  AND created_at >= :rollback_started_at
GROUP BY ticket_status
ORDER BY ticket_status;

SELECT count(*) AS invalid_ticket_consumption_count
FROM agent2_semantic_admission_tickets
WHERE tenant_id = :tenant_id
  AND (
      (
          ticket_status = 'consumed'
          AND (consumed_at IS NULL OR consumed_receipt_ref IS NULL)
      )
      OR (
          ticket_status <> 'consumed'
          AND (consumed_at IS NOT NULL OR consumed_receipt_ref IS NOT NULL)
      )
  );

SELECT count(*) AS invalid_ticket_claim_count
FROM agent2_semantic_admission_tickets
WHERE tenant_id = :tenant_id
  AND (
      segment_text_sha256 IS NULL
      OR segment_end_offset < segment_start_offset
      OR fact_claims_sha256 IS NULL
      OR authorized_command_sha256 IS NULL
      OR jsonb_typeof(authority_scope_json) <> 'object'
      OR jsonb_typeof(allowed_changed_fields_json) <> 'array'
      OR executor_revalidation_required IS NOT TRUE
      OR proves_business_write IS NOT FALSE
  );
```

Decision artifact integrity:

```sql
SELECT domain, operation, verdict, count(*)
FROM agent2_semantic_admission_decisions
WHERE tenant_id = :tenant_id
  AND user_id = ANY(:canary_user_ids)
  AND created_at >= :rollback_started_at
GROUP BY domain, operation, verdict
ORDER BY domain, operation, verdict;

SELECT count(*) AS invalid_decision_artifact_count
FROM agent2_semantic_admission_decisions
WHERE tenant_id = :tenant_id
  AND NOT (
      (
          verdict = 'admitted'
          AND operation IN (
              'query_daily_report',
              'query_periodic_report',
              'answer_case_query',
              'query_case_progress',
              'query_operation_status',
              'search_enterprise_knowledge'
          )
          AND ticket_id IS NULL
          AND pending_id IS NULL
      )
      OR (
          verdict = 'admitted'
          AND operation NOT IN (
              'query_daily_report',
              'query_periodic_report',
              'answer_case_query',
              'query_case_progress',
              'query_operation_status',
              'search_enterprise_knowledge'
          )
          AND object_type IS NOT NULL
          AND object_stable_id IS NOT NULL
          AND ticket_id IS NOT NULL
          AND pending_id IS NULL
      )
      OR (
          verdict = 'information_required'
          AND ticket_id IS NULL
          AND pending_id IS NOT NULL
      )
      OR (
          verdict IN ('blocked','no_op','review_only','deferred_audit_only')
          AND ticket_id IS NULL
          AND pending_id IS NULL
      )
  );
```

Audit-only invariants:

```sql
SELECT count(*) AS review_write_violation_count
FROM agent2_semantic_review_items
WHERE tenant_id = :tenant_id
  AND (audit_only IS NOT TRUE OR business_write_allowed IS NOT FALSE);

SELECT count(*) AS deferred_write_violation_count
FROM agent2_deferred_semantic_events
WHERE tenant_id = :tenant_id
  AND (
      audit_only IS NOT TRUE
      OR business_write_allowed IS NOT FALSE
      OR requires_fresh_admission IS NOT TRUE
  );
```

Digest-only Trace storage and UUIDv5 artifact identity:

```sql
SELECT count(*) AS forbidden_trace_raw_column_count
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND table_name = 'agent2_semantic_admission_traces'
  AND column_name IN ('proposal_json', 'raw_text', 'segment_text', 'message_text');

SELECT count(*) AS non_uuid_v5_artifact_count
FROM (
    SELECT trace_id AS artifact_id FROM agent2_semantic_admission_traces
    WHERE tenant_id = :tenant_id
    UNION ALL
    SELECT decision_id FROM agent2_semantic_admission_decisions
    WHERE tenant_id = :tenant_id
    UNION ALL
    SELECT ticket_id FROM agent2_semantic_admission_tickets
    WHERE tenant_id = :tenant_id
    UNION ALL
    SELECT review_id FROM agent2_semantic_review_items
    WHERE tenant_id = :tenant_id
    UNION ALL
    SELECT deferred_event_id FROM agent2_deferred_semantic_events
    WHERE tenant_id = :tenant_id
    UNION ALL
    SELECT pending_id FROM agent2_information_pendings
    WHERE tenant_id = :tenant_id
) artifacts
WHERE substring(artifact_id::text from 15 for 1) <> '5'
   OR lower(substring(artifact_id::text from 20 for 1)) NOT IN ('8','9','a','b');
```

All violation counts must be zero. Also verify existing business receipt and audit tables by authoritative Ticket ID and source-message correlation. Never infer a business write from Admission tables alone.

## Data retention

Normal rollback preserves all six tables:

- `agent2_semantic_admission_traces`
- `agent2_semantic_admission_decisions`
- `agent2_semantic_admission_tickets`
- `agent2_semantic_review_items`
- `agent2_deferred_semantic_events`
- `agent2_information_pendings`

Trace storage contains only proposal digests, not raw proposal text. Ticket `authority_scope_json`, Review candidate snapshots, Deferred payloads, and Pending question snapshots may contain protected business evidence; apply the project retention, encryption, role, and redaction policy. Do not emit those JSON fields into routine logs or user-facing evidence.

## Optional schema removal

Schema removal is not part of a normal production rollback. Use it only when all conditions below are met:

- application and workers have been rolled back and no deployed code references these tables;
- every Ticket and Information Pending is consumed, expired, cancelled, conflicted, or permission-revoked;
- audit/retention owners approved deletion and an encrypted export was verified;
- dependency inspection shows no unexpected views, foreign keys, or reporting jobs;
- exact database and tenant scope were independently reviewed.

Dependency check:

```sql
SELECT
    dependent_ns.nspname AS dependent_schema,
    dependent.relname AS dependent_object,
    source.relname AS admission_table
FROM pg_depend dependency
JOIN pg_class source ON source.oid = dependency.refobjid
JOIN pg_class dependent ON dependent.oid = dependency.objid
JOIN pg_namespace dependent_ns ON dependent_ns.oid = dependent.relnamespace
WHERE source.relname IN (
    'agent2_semantic_admission_traces',
    'agent2_semantic_admission_decisions',
    'agent2_semantic_admission_tickets',
    'agent2_semantic_review_items',
    'agent2_deferred_semantic_events',
    'agent2_information_pendings'
)
  AND dependent.oid <> source.oid
ORDER BY admission_table, dependent_schema, dependent_object;
```

Because Decision-to-Ticket and Decision-to-Pending references are intentionally circular and deferred, remove those two constraints first. Then remove tables in reverse dependency order. Do not use `CASCADE`.

```sql
BEGIN;

ALTER TABLE IF EXISTS agent2_semantic_admission_decisions
    DROP CONSTRAINT IF EXISTS agent2_semantic_admission_decision_ticket_fk;
ALTER TABLE IF EXISTS agent2_semantic_admission_decisions
    DROP CONSTRAINT IF EXISTS agent2_semantic_admission_decision_pending_fk;

DROP TABLE IF EXISTS agent2_information_pendings;
DROP TABLE IF EXISTS agent2_deferred_semantic_events;
DROP TABLE IF EXISTS agent2_semantic_review_items;
DROP TABLE IF EXISTS agent2_semantic_admission_tickets;
DROP TABLE IF EXISTS agent2_semantic_admission_decisions;
DROP TABLE IF EXISTS agent2_semantic_admission_traces;

COMMIT;
```

Any dependency failure is a stop signal requiring investigation, not permission to delete additional objects.

## Exit criteria

- Canary traffic is on the recorded known-good runtime or all Agent2 mutations are fail-closed.
- No command can bypass authoritative Ticket lookup and revalidation.
- Outstanding pre-rollback Tickets are invalidated or expired and cannot be consumed.
- No new business receipt is attributed to an expired, cross-scope, conflicted, revoked, cancelled, or reused Ticket.
- Review/Deferred artifacts remain audit-only and were not replayed.
- Admission audit evidence remains queryable and protected.
- Deployed runtime hash, Admission policy version, flags, route version, invalidation receipt, and rollback timestamp are captured in release evidence.
