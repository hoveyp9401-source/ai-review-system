from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.legal_ops.repository import SandboxRepository
from app.legal_ops.seed import build_phase0_seed


def main() -> int:
    parser = argparse.ArgumentParser(description="Legal Operations Phase 0 sandbox lifecycle")
    parser.add_argument("command", choices=("seed", "reset", "verify"))
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    settings = Settings()
    if not settings.legal_ops_sandbox_enabled:
        parser.error("LEGAL_OPS_SANDBOX_ENABLED=true is required; production mode is never accepted")
    repository = SandboxRepository(
        Path(settings.legal_ops_sandbox_data_path),
        sandbox_enabled=True,
    )
    seed = build_phase0_seed(settings.legal_ops_sandbox_seed_manifest)
    seed_id = seed["metadata"]["seed_id"]
    tenant_id = (
        args.tenant
        or settings.legal_ops_sandbox_default_tenant.strip()
        or seed["tenants"][0]["id"]
    )

    if args.command == "seed":
        current_metadata = repository.load()["metadata"] if repository.path.exists() else {}
        current_seed_id = current_metadata.get("seed_id")
        current_revision = current_metadata.get("generator_revision")
        expected_revision = seed["metadata"]["generator_revision"]
        if not repository.path.exists():
            result = repository.reset(seed)
        elif (current_seed_id, current_revision) == (seed_id, expected_revision):
            result = {
                "seed_id": current_seed_id,
                "generator_revision": current_revision,
                "status": "already_seeded",
            }
        else:
            parser.error(
                "existing snapshot revision differs; seed refuses a cross-tenant overwrite, use confirmed tenant reset"
            )
    elif args.command == "reset":
        if args.confirm != seed_id:
            parser.error(f"--confirm {seed_id} is required for reset")
        result = repository.reset_tenant(seed, tenant_id)
    else:
        result = repository.verify_tenant(tenant_id)

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
