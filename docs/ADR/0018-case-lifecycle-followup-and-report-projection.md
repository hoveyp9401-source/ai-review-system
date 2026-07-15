# ADR 0018: Persistent Case Follow-up orchestration with derived Report projection

Status: accepted for the current two-user Agent2 Canary.

Case lifecycle follow-up is implemented as a persistent policy/task/pending ledger whose scheduler creates typed tasks before an existing notification outbox performs transport. A natural-language reply is bound to one permission-checked Case Follow-up Pending and first produces committed Case receipts. Only then may an independently persisted Report Projection Request invoke the unified Report Domain; its failure cannot roll back the Case fact, and its correction cannot mutate that fact. We reject Scheduler-authored prompts as business tasks, LLM-selected database identities, direct Case-to-Daily dual writes, and tenant-wide Case scans because the Canary scope is the exact union of the two users' assigned 80 Case IDs rather than all 83 rows currently present in the tenant.
