# ADR 0014: Structured party knowledge base

Status: accepted for Phase 2 implementation.

Permission-joinable `PartyCaseClue` rows represent typed person, court, payment, asset, document and other clues with an exact party, case and source reference; they are not inferred from vector similarity. Every new `PartyRelation` is also case-scoped. Party queries return only relations whose source case is visible and whose related party appears in that same case, and only clues attached to a visible case. Legacy relation rows without `case_id` fail closed and are not queryable. This prevents a valid party match from becoming an oracle for unrelated people, courts, payments or assets.

Party identity facts live in PostgreSQL tables for entities, identifiers, aliases, case roles, relations, source references, merge candidates and conflicts. Party roles are case-scoped; no entity receives a permanent “plaintiff” or “defendant” label.

Resolution order is tenant/case permission, confirmed exact identifier, exact canonical name, confirmed alias, then `pg_trgm` candidates. Fuzzy candidates always require clarification and never cause an automatic merge. Every promoted fact retains a source reference and confirmation state. Vector retrieval may later recall unstructured materials but cannot establish identity facts.
