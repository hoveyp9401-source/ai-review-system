# ADR 0015: Travel collaboration and notification outbox

Status: accepted for Phase 2 implementation.

Travel matching is conservative: same tenant, company, department and city; different users; overlapping time; active status; confidence at least 0.85. Same-province-only input is not matchable. A sweep groups three or more concurrent travelers into one collaboration candidate instead of producing every pair.

Candidate and per-recipient notification idempotency keys prevent duplicate enqueue. Dispatch uses active DingTalk identity bindings, `FOR UPDATE SKIP LOCKED`, exponential retry, stale-lock recovery and dead-letter state. The transport receipt and append-only dispatch history are stored on the outbox row. Notifications disclose colleague display names, destination, overlapping dates and the collaboration question only.

The first version does not modify a formal travel application or book an itinerary.

