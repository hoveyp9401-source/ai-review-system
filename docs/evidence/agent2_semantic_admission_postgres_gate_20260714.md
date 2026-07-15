# Agent2 Semantic Admission PostgreSQL Isolated Gate

- Final status: `PASS`
- Run ID: `20260714_c615c680d28a`
- Started (UTC): `2026-07-13T20:13:52+00:00`
- Finished (UTC): `2026-07-13T20:13:55+00:00`
- Migration SHA-256: `ac96c379ae93852d54896f60dc14213e9974c7bd760fe01490a9cea0d79c1aaa`
- Verifier SHA-256: `563a53afef0d4332f10859a55c6e1842aaa85f5ba38f324f4b8cc2f7c55d171c`
- Execution transport: `ssh`
- Temporary schema: `agent2_admission_gate_20260714_c615c680d28a`
- Cleanup confirmed: `True`

## Reproducible gate results

| Gate | Result | Reproducible evidence |
|---|---:|---|
| Migration first apply | PASS | transaction completed |
| Migration idempotent re-apply | PASS | canonical catalog hashes equal |
| Six tables and constraints | PASS | tables=6; FKs=12 |
| Legal artifact roundtrip | PASS | synthetic records across all six tables |
| Illegal artifacts fail closed | PASS | cases=8; residue=0 |
| Transaction rollback | PASS | remaining rows=0 |
| Concurrent idempotency | PASS | committed rows=1 |
| Public catalog unchanged | PASS | metadata fingerprint before/after equal |
| Exact temporary-schema cleanup | PASS | schema absent after finally |

## Safety boundary

- The verifier used only synthetic identifiers and digests.
- No business row, production user identity, message, configuration, or service was written.
- `public` was read only for catalog metadata fingerprinting; the migration search path was the unique temporary schema followed by `pg_catalog`.
- This database gate does not claim a user-facing Agent2 end-to-end pass.

## Reproduce

Run this verifier with a new allow-listed `agent2_admission_gate_<unique>` schema name, the unchanged migration, and the server `.env`; pass `--execution-transport ssh` when it is invoked through the isolated SSH session. The verifier refuses a pre-existing schema and always performs exact cleanup in `finally`.
