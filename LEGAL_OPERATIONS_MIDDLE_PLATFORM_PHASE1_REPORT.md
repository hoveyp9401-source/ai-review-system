# Legal Operations Middle Platform Phase 0 Acceptance Report

Date: 2026-07-11  
Scope: first runnable Sandbox tenant, read-only aggregation, no production deployment  
Verdict: **READY_FOR_INTERNAL_DEMO**

## 1. Executive Summary

A runnable Legal Operations Sandbox now exists at `/legal-ops/`. It includes a unified desktop application shell, a tenant-isolated read-only BFF, configuration-driven fixtures, tenant-scoped Seed/Reset/Verify, independent daily/weekly/monthly source contracts, a backend metric registry, cross-module drilldowns, and a deep case lifecycle view.

The implementation is safe for an internal fixture-based demonstration. It is not declared `SANDBOX_MVP_READY` because no authenticated adapter has yet imported the current test company's real weekly, monthly, travel, performance, or case records, and field/document-level enterprise authorization is not complete. It is not production-ready.

## 2. Current System Baseline

The existing application is a FastAPI service backed by global PostgreSQL models for teams, users, daily reports, summaries, and performance submissions. Those models do not yet provide a complete tenant/company/department boundary. Existing case and travel capabilities create observe-only candidates and are not a formal operations read model. The repository has no standalone React/Vue/Next application.

To avoid coupling the Phase 0 work to production report, DingTalk, database, and Agent2 write paths, the implementation adds an isolated subsystem instead of mutating existing business tables.

## 3. Files Actually Changed

New implementation:

- `app/legal_ops/auth.py`
- `app/legal_ops/repository.py`
- `app/legal_ops/seed.py`
- `app/legal_ops/service.py`
- `app/legal_ops/api.py`
- `app/legal_ops/dev_app.py`
- `app/legal_ops/fixtures/phase0_manifest.json`
- `app/legal_ops/static/index.html`
- `app/legal_ops/static/styles.css`
- `app/legal_ops/static/app.js`
- `scripts/legal_ops_sandbox.py`

Integration and configuration:

- `app/config.py`
- `app/main.py`
- `.env.example`

Tests and documentation:

- `tests/test_legal_ops_sandbox.py`
- `tests/test_legal_ops_e2e.py`
- `docs/LEGAL_OPERATIONS_PHASE0_RUNBOOK.md`
- `docs/ADR/0013-legal-operations-phase0-sandbox-boundary.md`
- `LEGAL_OPERATIONS_MIDDLE_PLATFORM_PHASE1_REPORT.md`

No existing Agent2 runtime write path, legacy fallback, production database schema, or production business data was changed.

## 4. Target Architecture

```text
Browser App Shell
  -> /legal-ops/api/* BFF
     -> server-side PrincipalDirectory
     -> LegalOpsReadService
        -> tenant-filtered SandboxRepository
           -> ignored atomic JSON snapshot
              -> configuration-driven Phase 0 seed manifest
```

The standalone entrypoint imports neither the production database nor the DingTalk/Agent2 stacks. The same router is also mounted in the main FastAPI application but remains disabled by default.

## 5. Multi-Tenant and Authorization

Identity is derived from a server-side credential directory. `tenant_id` in a query string and `X-Tenant-Id` are never authorization authorities. Tenant filtering occurs before list aggregation, metric calculation, and detail lookup.

Supported role vocabulary includes:

- `legal_member`
- `case_owner`
- `team_lead`
- `department_head`
- `legal_center_manager`
- `tenant_reader`
- `tenant_admin`
- `sandbox_admin`
- `system_admin`

Non-wide principals are filtered by configured `team_ids`. A member scoped to `alpha-dispute` receives only that team's cases and receives `404` for an `alpha-contract` query. Tenant A receives `404` for Tenant B case identifiers.

Reset requires both an administrative role and an explicit seed-id confirmation. Reset replaces only the authenticated tenant partition; the other tenant snapshot is asserted unchanged.

## 6. Data Source Inventory

| Source | Adapter | Origin | Current mode | Production status |
|---|---|---|---|---|
| Case registry | `case_registry_adapter` | `imported_record` | Sandbox fixture, read-only | Not connected |
| Daily form | `daily_form_adapter` | `human_record` | Sandbox fixture, read-only | Contract only |
| Weekly form | `weekly_form_adapter` | `human_record` | Sandbox fixture, read-only | Contract only |
| Monthly form | `monthly_form_adapter` | `human_record` | Sandbox fixture, read-only | Contract only |
| Travel form | `travel_form_adapter` | `human_record` | Sandbox fixture, read-only | Contract only |
| Sandbox AI | `sandbox_ai_adapter` | `ai_summary` | Derived fixture | No fact promotion |
| External clue | `external_clue_adapter` | `external_clue` | Unconfirmed fixture | No asset verification |

Existing raw case indexes are not treated as trusted system facts and were not copied into the committed fixture.

## 7. Weekly and Monthly Source Design

Daily, weekly, and monthly submissions are separate collections with separate source IDs and adapters:

```text
daily   -> daily_form_adapter   -> daily_submissions
weekly  -> weekly_form_adapter  -> weekly_submissions
monthly -> monthly_form_adapter -> monthly_submissions
```

All three contracts return `depends_on: []`. Weekly does not read daily. Monthly reads neither daily nor weekly. Automated tests assert that daily and weekly source-record IDs are disjoint.

## 8. Adapters and BFF

The BFF exposes scoped endpoints for shell metadata, overview, reporting periods and raw submissions, teams, travel, metrics, cases and lifecycle detail, quality, sources, permissions, and tenant reset.

The service layer owns aggregation. The browser renders values and never calculates formal metrics. Typed failure behavior includes `401` for invalid credentials, `403` for role denial, `404` for resources outside the authorized scope, `409` for missing reset confirmation, and `422` for invalid route enums.

## 9. Frontend App Shell

The app shell provides a fixed navigation rail, tenant identity, team filter, fixture warning, loading state, error state, refresh action, detail drawer, and responsive fallback. It is served as local HTML/CSS/JavaScript to avoid adding an unconfigured Node build chain to this backend repository.

The UI uses the BFF token header. It has no tenant selector capable of changing authorization scope.

## 10. Shared Components

Implemented shared presentation patterns include:

- navigation and page heading;
- tenant/fixture banner;
- team filter;
- metric cards and server-aggregated bars;
- tables and status badges;
- source-contract callout;
- detail drawer;
- lifecycle lanes and program timeline;
- origin/AI/conflict badges;
- loading, error, empty, and permission failure surfaces.

The API contracts and enums are centralized in Python and returned consistently. A generated/shared TypeScript contract is deferred.

## 11. Test Company Seed

`phase0_manifest.json` is configuration, not application code. It defines two fixture tenants so that isolation is always testable. The first tenant has one company, one department, three teams, four users, 12 cases, independent reporting submissions, travel, metrics, quality issues, sources, and roles.

Every generated record has `fixture: true`, and snapshot metadata states that the content is synthetic. Company/team/user names and identifiers can be replaced by editing or generating a manifest; page and service code do not embed them.

No real current-test-company data is used in this Phase 0 build.

## 12. Home Page

The home page displays server-computed open cases, high-risk cases, active travel, quality issues, case status distribution, risk distribution, and representative cases. Cards route to filtered detail pages.

The home endpoint returns only summary plus six representative cases; it does not preload every lifecycle node for all cases.

## 13. Daily Report Center

The daily center reads only `daily_submissions`. It shows completion state by person/team and supports opening the raw submission. The detail drawer displays raw form text, source record, confirmation/origin state, and linked case IDs. Linked cases open the deep lifecycle drawer.

The Phase 0 daily records are fixtures, not existing production daily reports.

## 14. Weekly Dashboard

The weekly dashboard reads only `weekly_submissions` via `weekly_form_adapter`. It exposes summary counts, person/team rows, source-record IDs, raw submission detail, and an explicit `depends_on=[]` contract.

## 15. Monthly Dashboard

The monthly dashboard reads only `monthly_submissions` via `monthly_form_adapter`. It mirrors the raw-source drilldown contract and has no dependency on daily or weekly submissions.

## 16. Team Workbench

The team workbench shows member count, case load, and high-risk count by team. The global team filter narrows the view. Team cards drill to the authorized team's case list.

## 17. Travel Dashboard

Travel uses an independent read-only adapter. Each trip displays traveler, destination, dates, purpose, status, and related case. Related case IDs open the lifecycle view. No notification or official travel write is enabled.

## 18. Performance and Metric Center

Confirmed backend registry entries currently produce formal values for:

- `open_case_count`
- `high_risk_case_count`
- `active_travel_count`
- `daily_submission_rate`

Each formal value includes raw components with resource, team, user, and component value. `risk_resolution_rate` remains `draft`; `case_efficiency_index` remains `pending_business_confirmation`. Both return `value: null` and `warning: not a formal metric`. No invented formula is presented.

## 19. Case Center

The first tenant contains 12 representative cases across litigation, arbitration, execution, and non-litigation dispute types, multiple phases, statuses, risk levels, teams, owners, and amounts. The case list supports team, status, risk, and text filtering.

Case lookup is always tenant-scoped; cross-tenant identifiers are not disclosed.

## 20. Case Lifecycle Domain Model

Each case contains:

- one lifecycle root;
- five phase lanes: intake, filing, trial, execution, closure;
- an ordered program timeline;
- events;
- parties;
- documents/evidence;
- internal work records;
- execution assets;
- collection/finance records;
- risks and tasks;
- external clues;
- source references;
- AI-derived nodes;
- a conflicted amount node.

Every node includes tenant, case, parent, lane, kind, sequence, occurrence time, state, fixture flag, and provenance.

## 21. Case Lifecycle Page

The case drawer renders a program timeline plus five separately browsable lanes. It does not reduce the lifecycle to a single timeline or relationship graph. Documents, execution, collection, risks, tasks, external clues, and closure records are visible within their phases.

AI nodes use blue styling and retain generator, generated time, confidence, confirmation state, reviewer, and source. Conflicted data has a separate `conflicted` status.

## 22. Lifecycle Sample Cases

Tenant `sandbox-alpha` has `alpha-case-01` through `alpha-case-12`. Each has 16 lifecycle nodes, including five program events on the timeline. The samples cover different current phases and provide vertical traceability from case list to lifecycle, source, reporting submission, travel, quality issue, and metric component where applicable.

Tenant `sandbox-beta` has two separate cases used only for isolation validation.

## 23. Data Quality

The quality center presents:

- unconfirmed AI output;
- pending external clues that must not be treated as verified assets;
- conflicting amount definitions;
- tenant reference verification and violation count.

Allowed provenance statuses are published by the source center: `system_fact`, `human_record`, `imported_record`, `ai_extracted`, `ai_summary`, `ai_inference`, `ai_suggestion`, `external_clue`, `unknown`, and `conflicted`.

## 24. Cross-Module Drilldowns

Implemented drilldowns:

- home card -> filtered case/travel/quality view;
- daily/weekly/monthly row -> raw source submission;
- daily raw submission -> related case;
- case -> related daily source IDs;
- case -> related travel;
- travel -> related case;
- metric -> team/user/raw components;
- case node -> provenance displayed in the lifecycle;
- quality issue -> resource/source identifiers.

Unsupported production evidence/document downloads are not simulated.

## 25. Test Results

Focused acceptance command:

```text
python -m pytest tests/test_legal_ops_sandbox.py tests/test_legal_ops_e2e.py -q
36 passed
```

The 18 numbered E2E tests map one-to-one to section 28.3 of the request: leader filters, daily/team/person/raw text, daily-to-case, lifecycle, legal backbone, event evidence, execution/collection/risk/task, return to daily, weekly raw source, monthly raw source, metric drilldown, travel-to-case, cross-tenant denial, team denial, and reset recovery.

Additional checks passed:

```text
python -m compileall -q app/legal_ops scripts/legal_ops_sandbox.py
node --check app/legal_ops/static/app.js
git diff --check -- <Phase 0 files>
```

Browser smoke verified login, independent weekly contract, daily raw detail, daily-to-case navigation, lifecycle lanes/nodes/timeline, AI/conflict labels, formal metric components, team filtering, and zero application console warnings/errors.

The repository-wide `python -m pytest -q` command was also attempted. Collection stopped in 18 pre-existing test modules because the active system Python does not have the repository's PostgreSQL driver `asyncpg`; no test body ran in that invocation. This is recorded as an environment blocker, not a green full-suite result. The standalone Sandbox and its 36 focused tests do not import or require that driver.

The final two-axis review found no remaining Standards P0/P1/P2 and no remaining Spec P0/P1/P2 other than the explicitly documented strict-TypeScript contract deferral.

## 26. Multi-Tenant Isolation Results

Verified properties:

- Tenant A case queries return only `sandbox-alpha` records.
- Tenant A cannot load `beta-case-01`.
- URL and `X-Tenant-Id` tampering do not change the server principal.
- A team-scoped member cannot query `alpha-contract`.
- Lifecycle node tenant IDs and source references are validated.
- Tenant-only reset preserves Tenant B's complete snapshot.
- Seed and reset are disabled unless `LEGAL_OPS_SANDBOX_ENABLED=true`.
- Startup refuses to overwrite an unreadable or revision-mismatched snapshot; migration requires an explicit tenant reset.
- Unique temporary files and an OS-level lock serialize concurrent CLI/server reset writers.

The CLI verification returned `valid: true` with zero violations for both seeded tenants.

## 27. Page Screenshots

Generated visual records are stored under `outputs/legal_ops_phase0/` (ignored by Git because they contain runtime artifacts):

- `overview-viewport.png`
- `daily-detail.png`
- `weekly-dashboard.png`
- `monthly-dashboard.png`
- `team-workbench.png`
- `travel-dashboard.png`
- `metrics-center.png`
- `metric-drilldown.png`
- `case-list.png`
- `case-lifecycle.png`
- `case-lifecycle-timeline.png`
- `data-quality.png`

The lifecycle screenshots jointly show timeline, event sources, documents/evidence, execution assets, collection, risks, tasks, AI nodes, and conflicted data. A browser full-page capture was discarded from acceptance evidence because the in-app compositor distorted it; viewport captures and DOM assertions are the accepted records.

## 28. Known Issues

1. All module data is synthetic fixture data. No real test-company adapter has been authenticated or imported.
2. The frontend is typed by stable BFF contracts but is plain JavaScript; strict TypeScript and a generated shared contract are deferred.
3. The list size is deliberately small, so pagination and virtualization are not implemented yet.
4. Field-level authorization for case amounts, documents, exports, and AI review queues is not yet modeled beyond tenant/team scope.
5. Real document/evidence file storage and download are not connected.
6. The metric registry has four confirmed demonstration metrics; business-owned formulas and version history are incomplete.
7. The existing main application requires its normal PostgreSQL driver/runtime. The standalone Sandbox entrypoint avoids that dependency for Phase 0 demos.
8. Visual tests are reproducible browser records, not yet a committed pixel-diff CI suite.

## 29. External Blockers

The following inputs are required before `SANDBOX_MVP_READY` can be declared:

- authoritative current test-company tenant/company/department/team/user mapping;
- authenticated daily, weekly, monthly, travel, performance, and case source endpoints or exports;
- business confirmation and owners for formal metric definitions;
- document/evidence access rules and retention policy;
- enterprise identity/SSO and role assignments;
- confirmation that encoded legacy case-index fields are safe and correctly decoded for import.

These are external data/governance inputs, not reasons to bypass isolation or label fixtures as real.

## 30. Acceptance Decision

| Criterion | Result | Evidence |
|---|---|---|
| Runnable, seedable, resettable, verifiable | Pass | runbook, CLI, standalone app |
| Complete core pages | Pass for fixture demo | 11 navigation entries and screenshots |
| Daily/weekly/monthly source independence | Pass | separate collections/adapters, tests |
| Deep lifecycle | Pass | 12 cases, 13 nodes, 5 lanes, timeline |
| Cross-module drilldown | Pass for fixture scope | browser and 18 E2E tests |
| Multi-tenant isolation | Pass for Phase 0 model | server principal, team scope, tenant reset |
| AI/fact distinction | Pass | provenance contract and UI badges |
| Real test-company data | Not met | fixtures only |
| Production readiness | Out of scope / not met | intentionally disabled |

Final decision: **READY_FOR_INTERNAL_DEMO**.

The result is stronger than a static prototype and supports a complete fixture-based walkthrough. It must not be promoted as `SANDBOX_MVP_READY` or production-ready until the external blockers in section 29 are closed and their adapters are re-run through the same acceptance suite.

## 31. Minimum Next Actions

1. Produce the canonical test-company organization/identity manifest and load it through the existing seed contract.
2. Implement one authenticated, read-only real adapter end-to-end, starting with case registry or weekly submissions.
3. Add adapter schema validation, import ledger, deduplication, stale/partial markers, and source-version audit.
4. Confirm metric definitions with business owners and add versioned registry entries rather than frontend formulas.
5. Add field/document/export authorization and tests for every role.
6. Move BFF contracts to generated strict TypeScript types and add frontend unit/accessibility tests.
7. Add pagination/lazy lifecycle evidence loading before increasing sample or real record volume.
8. Re-run the 18 E2E scenarios with the real test-company Sandbox tenant, capture updated screenshots, and reconsider `SANDBOX_MVP_READY`.
