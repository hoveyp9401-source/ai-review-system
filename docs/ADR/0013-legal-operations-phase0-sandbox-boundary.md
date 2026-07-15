# ADR 0013: Legal Operations Phase 0 Sandbox Boundary

## Status

Accepted for Phase 0.

## Context

The repository's production application uses a global PostgreSQL model without a complete tenant/company/department boundary. Directly extending those tables would risk accidental coupling to production report, bot, and Agent2 workflows.

## Decision

Build a separate `app.legal_ops` subsystem with a read-only BFF, an atomic JSON Sandbox repository, a configuration-driven seed manifest, and a standalone FastAPI entrypoint. The integrated application may mount the same router, but the subsystem remains disabled by default.

Identity scope is resolved from a server-side credential directory. Tenant/company/team values supplied by the browser are filters only and cannot grant access. Tenant filtering is applied before every service aggregation and detail lookup. Sandbox Seed and Reset require explicit enablement; Reset additionally requires an administrator role and seed-id confirmation.

Daily, weekly, and monthly data each have an independent adapter contract. Formal metrics are emitted only for confirmed definitions with a registered backend calculator. AI-derived content is represented by provenance metadata and never promoted to a system fact without confirmation.

## Consequences

- Phase 0 can be run and reset without a production database.
- A second tenant is always present for isolation tests.
- Existing Agent2 write safety boundaries remain untouched.
- The Sandbox is suitable for internal demonstration, not production deployment.
- Production onboarding later requires persistent tenant-aware tables, enterprise identity integration, field-level authorization, and real source adapters behind the same BFF contracts.
