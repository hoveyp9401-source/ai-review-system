# Legal Operations Middle Platform — Phase 0 Runbook

This runbook starts only the isolated Legal Operations Sandbox. It does not import the production database, DingTalk bot, or Agent2 write paths.

## Configure

PowerShell:

```powershell
$env:LEGAL_OPS_SANDBOX_ENABLED='true'
$env:LEGAL_OPS_SANDBOX_TOKEN='replace-with-a-local-secret'
$env:LEGAL_OPS_SANDBOX_DEFAULT_TENANT='sandbox-alpha'
```

For more than one tenant or role, set `LEGAL_OPS_SANDBOX_PRINCIPALS_JSON` to a server-side JSON object whose keys are credentials and whose values contain `tenant_id`, `user_id`, `role_ids`, and optional company/department/team scopes. Request query parameters and `X-Tenant-Id` are never identity authorities.

## Seed

```powershell
python scripts/legal_ops_sandbox.py seed
```

The command is idempotent and refuses to run unless `LEGAL_OPS_SANDBOX_ENABLED=true`.

## Start

```powershell
python -m uvicorn app.legal_ops.dev_app:app --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/legal-ops/` and enter the configured Sandbox token.

## Verify

```powershell
python scripts/legal_ops_sandbox.py verify --tenant sandbox-alpha
python -m pytest tests/test_legal_ops_sandbox.py tests/test_legal_ops_e2e.py -q
node --check app/legal_ops/static/app.js
```

## Reset

Read the current `seed_id` from `app/legal_ops/fixtures/phase0_manifest.json`, then provide it explicitly:

```powershell
python scripts/legal_ops_sandbox.py reset --confirm legal-ops-phase0-v6
```

The API equivalent is `POST /legal-ops/api/admin/reset` with a `tenant_admin` credential and `X-Legal-Ops-Reset-Confirm`. Reset replaces only the authenticated tenant partition, preserves other tenant snapshots, writes only the ignored Sandbox JSON file, and never reaches the production database.

## Data classes

- Every generated object has `fixture: true`.
- Human, imported, AI, external, unknown, and conflicted origins remain distinct.
- AI-derived nodes include generator, timestamp, confidence, confirmation state, reviewer, and source.
- Daily, weekly, and monthly submissions use separate adapters and source records.
- Confirmed metrics are computed by the backend registry; draft definitions return no formal value.
