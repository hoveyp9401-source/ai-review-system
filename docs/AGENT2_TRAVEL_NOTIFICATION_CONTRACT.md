# Agent2 travel notification contract

Message type: `travel_collaboration_question`.

Allowed content:

- colleague display name(s);
- canonical city;
- overlapping start/end dates;
- whether collaboration is desired.

Forbidden content:

- case names or case numbers;
- clients, amounts or assets;
- sensitive travel purpose;
- unrelated calendar entries.

Delivery state is `pending -> processing -> sent` or `failed -> retry -> dead_letter`. Each row has a tenant-scoped idempotency key, lock owner/time, retry count/time, external message ID, raw transport response and dispatch history. Only tenants in `AGENT2_BUSINESS_TENANT_IDS` can be claimed by the worker.

Candidates are created only when tenant, company, department, team and city all match. Candidate responses repeat the same organization checks against the channel-derived identity; a participant ID alone is insufficient authorization.

Every transport attempt also writes a `dispatch_travel_notification` business receipt and audit event in the same database transaction as the outbox state transition. The attempt key is stable per tenant, notification and attempt number. Successful attempts close as `executed`; retryable and dead-letter attempts close as `failed` with `notification_dispatch_failed` at the `transport` stage. Replaying the same attempt cannot add a second receipt or audit row.

The worker commits `processing` before network I/O and does not hold the claim lock while calling DingTalk. Ordinary transport failures are retryable. A stale `processing` row has an unknown delivery outcome, so it is moved to `dead_letter` with `delivery_outcome_unknown_after_stale_processing` and is never automatically resent; this favors the no-duplicate guarantee over an unsafe blind retry. An operator must reconcile that row with transport evidence before any manual replay.

Responses are limited to `accept`, `decline`, `later`, `changed` and `cancel`. The candidate becomes `accepted` only after every participant accepts. Decline closes only the candidate. Changed/cancel closes the responding actor's represented TravelIntent, invalidates related candidates and cancels any pending or failed notifications.
