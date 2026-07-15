# Agent2 Phase 2 typed command schemas

Status: normative implementation contract for the test-tenant Phase 2 path.

The Cognitive Core may emit semantic candidates only. It does not write a database and no executor accepts user raw text. Canonicalization and deterministic domain policy must produce one of the commands below or an explicit `PlanningBlock`.

## Shared execution context

Every business command is executed with a server-created `BusinessCommandContext`:

```text
tenant_id: non-empty string
company_id: string
department_id: string
team_id: string
actor_user_id: non-empty string
actor_role_ids: string[]
allowed_case_ids: string[]
source_message_id: non-empty string
source_channel: non-empty string
occurred_at: timezone-aware datetime
```

The context is derived from the active channel identity binding. Text supplied by the user cannot override identity, tenant, organization, role or case scope.

## Daily commands

All daily operations use the same closed envelope:

```text
command_id: UUID
decision_id: UUID
sub_decision_id: UUID
command_type: DailyCommandType
report_id: UUID
report_version: integer
target_item_ids: string[]
patch: object
idempotency_key: non-empty string
```

| `command_type` | Targets | Patch | Deterministic rule |
|---|---:|---|---|
| `append_item` | 0 | `{field, items[]}` | `field` is `today_work`, `problems` or `tomorrow_plan`; all items are non-empty |
| `edit_item` | 1 | `{replacement}` | item ID must resolve exactly once |
| `delete_item` | 1 | `{}` | soft business mutation through the report adapter; no text target |
| `merge_items` | 2+ | optional `{replacement}` | all IDs must exist in the same report field |
| `submit_report` | 0 | `{}` | report must be collecting and all three sections complete |
| `query_report` | 0 | `{report_date}` | read-only; exact date/ID/version come from a trusted persisted snapshot; no write lock or report upsert |
| `copy_report` | 0 | `{sections, source_report_date, source_report_id}` | source is a trusted snapshot; section keys are from the three-field vocabulary; values are string arrays |
| `clear_report` | 0 | `{field}` | field is one report field or `all` |
| `reopen_report` | 0 | `{report_date}` | exact persisted report status must be `completed` |

Every command checks report ownership, exact version, idempotency, state and stable item IDs. Instruction fragments, questions and chat text are forbidden payloads. Whole-report clear is emitted only after a uniquely bound pending confirmation; section clear is an exact-field action. Current-work projection and previous-plan completion compile to `copy_report` with source provenance.

## Travel commands

### `CreateTravelIntent`

```text
command_id: string
destination_raw: string
destination_normalized: non-empty city name
city_code: non-empty city code
province_code: string
start_at: timezone-aware datetime
end_at: timezone-aware datetime
time_precision: string
purpose_summary: string
related_case_ids: string[]
confidence: number
```

Policy requires a resolved city, `start_at <= end_at`, confidence at least `0.85`, and permission for every related case. The executor stores channel-derived identity and organization fields.

### `UpdateTravelIntent`

```text
command_id: string
travel_intent_id: string
expected_version: integer
start_at?: timezone-aware datetime
end_at?: timezone-aware datetime
destination_normalized?: string
city_code?: string
status?: proposed | planned | confirmed | changed | cancelled | completed
```

Only the owning channel identity may update the intent. A successful change increments the version and invalidates all non-terminal collaboration candidates and unsent notifications that include the old intent.

### `RespondTravelCollaboration`

```text
command_id: string
candidate_id: string
response: accept | decline | later | changed | cancel
```

The actor must be a participant and the candidate must not be terminal. One acceptance produces `accepted_by_one`; every participant must accept before `accepted`. Decline closes the candidate without cancelling travel. Changed/cancel closes the actor's represented intent and invalidates related candidates. Closed candidates reject later responses.

## Case progress commands

### `CreateCaseProgress`

```text
command_id: string
case_id: string
occurred_at: timezone-aware datetime
progress_type: string
summary: non-empty string
details: string
related_party_ids: string[]
related_document_ids: string[]
related_travel_intent_ids: string[]
confidence: number
```

The case must resolve uniquely inside `allowed_case_ids`. Explicit user records are stored as `human_record`; they are not promoted to formal court facts.

### `UpdateCaseProgress`

```text
command_id: string
progress_id: string
expected_version: integer
summary?: string
details?: string
```

At least one replacement is required. The target must resolve uniquely, be visible, not deleted, and be owned by the reporter unless the actor has `case_progress_admin`.

### `DeleteCaseProgress`

```text
command_id: string
progress_id: string
expected_version: integer
reason: string
```

Delete is a versioned soft delete. `deleted_at`, `deleted_by` and `delete_reason` remain queryable for audit.

### `QueryCaseProgress`

```text
command_id: string
case_id: string
start_at?: timezone-aware datetime
end_at?: timezone-aware datetime
```

This is read-only and returns only non-deleted progress inside the tenant and case permission scope.

### `LinkCaseProgress`

```text
command_id: string
progress_id: string
expected_version: integer
related_party_ids: string[]
related_document_ids: string[]
related_travel_intent_ids: string[]
```

Links are set-union updates under the same ownership, permission and optimistic-version rules as other progress writes.

## Party query command

### `QueryPartyCases`

```text
command_id: string
party_id: string
match_basis: exact_identifier | exact_canonical_name | confirmed_alias
role_type: string
include_recent_progress: boolean
```

Only an exact confirmed resolution may produce this command. `pg_trgm` results produce a clarification block, never this query. Results are tenant- and allowed-case-filtered and include match basis, case roles, status counts, visible-party relations, typed person/court/payment/asset/document clues and source references. Relations require their own `case_id` to be visible and the related party to occur in that same case; legacy relations without a case fail closed. Clues require the exact resolved party and a visible case.

## Receipt and audit closure

Every business command reserves an idempotency key before execution and returns a receipt with:

```text
receipt_id
command_id
command_type
tenant_id
actor_user_id
source_message_id
idempotency_key
status: executed | duplicate | blocked | failed
resource_type
resource_id
before
after
error_code
failed_stage
actual_write
created_at
```

A successful write creates an audit event in the same transaction with actor, source channel/message, tenant, resource and before/after snapshots. A blocked command records a typed `error_code` and `failed_stage` in its receipt and performs no domain write. A duplicate returns the original closure and performs no second write.

## Cross-domain invariant

Commands derived from the same user message share `source_message_id` but retain separate idempotency keys, receipts, transactions and outcomes. A Daily success cannot imply CaseProgress or Travel success, and one ambiguous business segment cannot erase a valid sibling-domain command.

Typed Daily execution persists a deterministic receipt in `agent2_daily_command_receipts` for executed, duplicate and blocked outcomes. The receipt binds tenant, actor, report/date, semantic decision IDs, exact typed command ID, idempotency key, before/after snapshots, validation outcome, actual-write flag and audit payload. Database receipt uniqueness is the replay authority: executed receipts replay as duplicate without a report upsert, while blocked receipts stay blocked for that key. Conversation state advances only when returned receipt command IDs exactly match every planned Daily and Business command.
