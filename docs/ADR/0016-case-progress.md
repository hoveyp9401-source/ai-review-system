# ADR 0016: Case progress as an auditable business record

Status: accepted for Phase 2 implementation.

An explicit user statement that uniquely resolves to an authorized case compiles to `CreateCaseProgress` with `content_origin=human_record`. It is not upgraded to a formal court fact. Ambiguous or unauthorized targets fail closed; the resolver never chooses the most recent or last case.

Create, update, link, query and soft delete use tenant/case permission, reporter/admin authorization, optimistic versions, idempotency keys, source message/channel, receipts and audits. Delete preserves the row, actor, time and reason. Daily and case-progress writes have independent receipts and may succeed or fail independently.

