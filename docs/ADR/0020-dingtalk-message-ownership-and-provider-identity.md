# ADR 0020: DingTalk Message Ownership and Provider Identity

- Status: Accepted
- Date: 2026-07-15
- Scope: production DingTalk Webhook/Stream ingress, Agent1/Agent2 routing, reminder dispatch evidence

## Context

The production service can receive one DingTalk message through HTTP Webhook or
Stream mode. Historically those transports generated different idempotency
keys (`dingtalk:*` and `dingtalk-stream:*`). The database only made the
application idempotency key unique, so a concurrent cross-transport delivery or
a delayed replay of an old Stream event could create two business turns.

Runtime ownership also had two control planes: the Phase 2 tenant route and the
legacy Agent2 daily-report user flag. A user routed to Agent1 by the Phase 2
control plane could still be claimed by the legacy Agent2 daily path. That made
the system vulnerable to competing interpretations even when each individual
path was internally correct.

Finally, scheduled report reminders discarded DingTalk's provider reference.
The application could say that it sent a reminder without retaining evidence
that DingTalk accepted the request.

## Decision

1. A DingTalk provider message has one canonical application key, independent
   of transport. The provider message ID is authoritative; a bounded SHA-256
   fingerprint is used only when the provider ID is missing.
2. PostgreSQL additionally enforces one row for
   `(platform, external_message_id)` when the external ID is present. Because
   the legacy `webhook_events` table belongs to another database role, the
   constraint lives in an application-owned `message_ingress_claims` ledger.
   The migration backfills every historical event, and the repository must
   atomically acquire the claim before it creates a new event.
3. When the Phase 2 control plane is enabled, its route is authoritative:
   `agent2_primary` is owned by Agent2, `blocked` is fail-closed, and every other
   route is owned by Agent1. The legacy Agent2 daily flag is considered only
   when the Phase 2 control plane is disabled.
4. The resolved route audit is committed before semantic/runtime execution.
   A later model, repository, or reply failure cannot erase the ownership
   evidence.
5. Reminder dispatch is recorded as `accepted_by_provider` only when DingTalk
   returns a provider reference. A provider reference is not described as a
   provider message ID or delivery confirmation. If a successful-looking
   response lacks evidence, the application fails closed and does not retry on
   another channel, because the first channel may already have accepted it.

## Consequences

- Webhook and Stream cannot both create a business turn for the same provider
  message after the claim-ledger migration is applied.
- Agent1 and Agent2 no longer compete for a message while the Phase 2 route
  plane is active.
- Historical rows keep their old application keys, but the provider-identity
  index prevents a delayed replay from creating a new row.
- Route audits become a durable join point from provider event to runtime owner
  and downstream receipt/outcome.
- Reminder evidence distinguishes provider acceptance from delivery. Existing
  historical reminders without provider evidence remain unproven; the change
  does not manufacture evidence retroactively.

## Rollback

1. Disable the affected service entrypoints or restore the pre-deploy files.
2. Restart API, Stream, and Scheduler from the recorded deployment snapshot.
3. The ingress claim ledger should normally remain in place because it is the
   database authority for message ownership. If removal is required, first
   restore code that does not depend on it, stop both ingress services, and
   explicitly drop `message_ingress_claims` only after recording its audit.
4. Do not re-enable legacy Agent2 daily ownership while the Phase 2 route plane
   is enabled.
