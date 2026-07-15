from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.business.entrypoint import parse_tenant_allowlist
from app.agent2.business.party_import import (
    build_party_import_plan,
    dataset_from_dict,
    persist_party_import_plan,
)
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.utils.time import now_in_timezone


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan or apply a tenant-scoped Agent2 Party import")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--actor-user-id", required=True)
    parser.add_argument("--source-message-id", default="manual-party-import")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-sandbox-tenant", default="")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    dataset = dataset_from_dict(payload)
    if dataset.tenant_id != args.tenant_id:
        raise SystemExit("input tenant_id does not match --tenant-id")
    plan = build_party_import_plan(dataset)
    preview = {
        "mode": "apply" if args.apply else "dry_run",
        "dataset_id": dataset.dataset_id,
        "tenant_id": dataset.tenant_id,
        "counts": plan.counts(),
        "merge_candidate_ids": [str(item.candidate_id) for item in plan.merge_candidates],
        "conflict_ids": [str(item.conflict_id) for item in plan.conflicts],
    }
    if not args.apply:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    allowed_tenants = parse_tenant_allowlist(settings.agent2_business_tenant_ids)
    if not settings.agent2_business_phase2_enabled:
        raise SystemExit("AGENT2_BUSINESS_PHASE2_ENABLED must be true before apply")
    if dataset.tenant_id not in allowed_tenants:
        raise SystemExit("tenant is not in AGENT2_BUSINESS_TENANT_IDS")
    if args.confirm_sandbox_tenant != dataset.tenant_id:
        raise SystemExit("--confirm-sandbox-tenant must exactly match the imported tenant")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await persist_party_import_plan(
                session,
                plan,
                actor_user_id=args.actor_user_id,
                source_message_id=args.source_message_id,
                occurred_at=now_in_timezone(settings.timezone),
            )
    print(
        json.dumps(
            {
                **preview,
                "receipt_id": result.receipt_id,
                "status": result.status,
                "actual_write": result.actual_write,
                "persistence_counts": result.counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_args())))
