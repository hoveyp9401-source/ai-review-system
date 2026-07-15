from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.business.models import Agent2IdentityBinding, RouteControlAudit
from app.db import AsyncSessionLocal


CONFIRMATION = "ROLLBACK_CASE_PROGRESS_COLLABORATION"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore identity permission scopes from an audited collaboration grant."
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--grant-source-message-id", required=True)
    parser.add_argument("--actor-user-id", required=True)
    parser.add_argument("--source-message-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    if args.apply and args.confirm != CONFIRMATION:
        raise SystemExit(f"--confirm must equal {CONFIRMATION}")
    async with AsyncSessionLocal() as session:
        audits = list(
            (
                await session.scalars(
                    select(RouteControlAudit).where(
                        RouteControlAudit.tenant_id == args.tenant_id,
                        RouteControlAudit.source_message_id
                        == args.grant_source_message_id,
                        RouteControlAudit.reason
                        == "grant_explicit_case_progress_collaboration",
                    )
                )
            ).all()
        )
        if not audits:
            raise SystemExit("audited collaboration grant not found")
        by_user = {
            str((item.after_json or {}).get("binding_user_id") or ""): item
            for item in audits
        }
        if "" in by_user or len(by_user) != len(audits):
            raise SystemExit("grant audit does not identify unique bindings")
        bindings = list(
            (
                await session.scalars(
                    select(Agent2IdentityBinding)
                    .where(
                        Agent2IdentityBinding.tenant_id == args.tenant_id,
                        Agent2IdentityBinding.user_id.in_(tuple(by_user)),
                        Agent2IdentityBinding.active.is_(True),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if len(bindings) != len(by_user):
            raise SystemExit("one or more granted bindings is no longer active")
        result: list[dict[str, object]] = []
        for binding in bindings:
            audit = by_user[binding.user_id]
            expected = dict(audit.after_json or {})
            before = dict(audit.before_json or {})
            current = dict(binding.permission_scope_json or {})
            if current.get("allowed_case_ids") != expected.get("allowed_case_ids"):
                raise SystemExit("visible scope changed after grant; refusing rollback")
            if current.get("writable_case_ids") != expected.get("writable_case_ids"):
                raise SystemExit("write scope changed after grant; refusing rollback")
            restored = dict(current)
            restored["allowed_case_ids"] = list(before.get("allowed_case_ids") or [])
            previous_writable = list(before.get("writable_case_ids") or [])
            if previous_writable:
                restored["writable_case_ids"] = previous_writable
            else:
                restored.pop("writable_case_ids", None)
            restored.pop("case_progress_collaboration_mode", None)
            result.append(
                {
                    "user_id": binding.user_id,
                    "display_name": binding.display_name,
                    "current_visible_count": len(current.get("allowed_case_ids") or []),
                    "restored_visible_count": len(restored.get("allowed_case_ids") or []),
                    "current_writable_count": len(current.get("writable_case_ids") or []),
                    "restored_writable_count": len(restored.get("writable_case_ids") or []),
                }
            )
            if args.apply:
                binding.permission_scope_json = restored
                session.add(
                    RouteControlAudit(
                        tenant_id=args.tenant_id,
                        actor_user_id=args.actor_user_id,
                        source_message_id=args.source_message_id,
                        before_json={
                            "binding_user_id": binding.user_id,
                            "allowed_case_ids": current.get("allowed_case_ids") or [],
                            "writable_case_ids": current.get("writable_case_ids") or [],
                        },
                        after_json={
                            "binding_user_id": binding.user_id,
                            "allowed_case_ids": restored.get("allowed_case_ids") or [],
                            "writable_case_ids": restored.get("writable_case_ids") or [],
                        },
                        reason="rollback_explicit_case_progress_collaboration",
                    )
                )
        if args.apply:
            await session.commit()
        else:
            await session.rollback()
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "tenant_id": args.tenant_id,
                "users": sorted(result, key=lambda item: str(item["display_name"])),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_arguments())))
