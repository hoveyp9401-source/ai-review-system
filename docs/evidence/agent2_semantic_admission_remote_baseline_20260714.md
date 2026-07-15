# Agent2 Semantic Admission remote read-only baseline — 2026-07-14

Evidence class: remote read-only observation before this phase is deployed.

Scope:

- host role: current production Agent2 host;
- repository: `/home/ai_review_tunnel/ai-review-system`;
- tenant: `sandbox-agent2-phase2-20260711`;
- observation was read-only; no migration, configuration edit, service restart, or business write was performed.

## Runtime baseline

- Git HEAD: `96cecec81c4ac9fe4e0500c08a52e661d094147d`.
- Remote repository was already dirty and contains extensive tracked and untracked work. It must not be treated as a clean checkout or overwritten wholesale.
- Python: 3.11.6.
- Running production process shape observed from the process table:
  - one Stream process;
  - one Scheduler process;
  - one API process on port 8000.
- A separate staging API process on port 8010 was also present and is outside the production deployment target.
- User-session `systemctl` was unavailable in the non-interactive SSH session, so the process table is the evidence source for this baseline.

Observed production hashes before this phase:

| File | SHA-256 |
|---|---|
| `app/agent2/cognitive_runtime_v3.py` | `2de9244f3c2bcba79ee0e798cefc0d4518552fbed1407c9e8fab9e2ef500d83d` |
| `app/api/webhook.py` | `b9579319fce89fc9ae1f346bdf731e994a5d759744e15c5284d7058902adabad` |
| `app/stream_runner.py` | `20b68a54244d6284f45af771906a4a70730f2dbeb83fbf5b666a448cb7663985` |
| `app/agent2/turn_runtime.py` | absent from the hash result at this baseline |

## PostgreSQL baseline

The query ran in an explicit read-only transaction against the application's configured PostgreSQL database and current schema `public`.

All six Phase 1 tables were absent:

- `agent2_semantic_admission_traces`;
- `agent2_semantic_admission_decisions`;
- `agent2_semantic_admission_tickets`;
- `agent2_semantic_review_items`;
- `agent2_deferred_semantic_events`;
- `agent2_information_pendings`.

Therefore no later row in those tables may be described as pre-existing evidence.

## Feature boundary baseline

Observed effective settings:

- Cognitive Core V3: enabled;
- business Phase 2: enabled;
- case-progress capability and writes: enabled;
- travel capability and writes: enabled;
- case-follow-up capability: enabled;
- both case-follow-up send controls: disabled;
- automatic case-follow-up report projection: disabled;
- business and case-follow-up tenant boundary: `sandbox-agent2-phase2-20260711`;
- case-follow-up user boundary contains exactly two internal user IDs;
- Cognitive Core model: `deepseek-v4-flash`, thinking disabled.

The two internal Canary user IDs are intentionally not duplicated in this evidence document; the deployed configuration remains the authoritative protected source. The admission rollout must reuse that exact two-user set and must keep follow-up send and automatic projection disabled.

## Consequence

Deployment must be a file-level, hash-checked merge into a dirty remote tree. A clean-tree checkout, repository-wide copy, destructive reset, or claim that Admission was already active would be incorrect.
