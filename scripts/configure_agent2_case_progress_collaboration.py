from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import UUID

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.business.entrypoint import parse_tenant_allowlist
from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    RouteControlAudit,
)
from app.config import get_settings
from app.db import AsyncSessionLocal


CONFIRMATION = "GRANT_CASE_PROGRESS_COLLABORATION"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or grant explicit case-progress create permission across a "
            "small set of existing Agent2 identity scopes."
        )
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--user-id", action="append", required=True)
    parser.add_argument("--actor-user-id", required=True)
    parser.add_argument("--source-message-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


def _uuid_scope(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    normalized: list[str] = []
    for value in values:
        try:
            parsed = str(UUID(str(value)))
        except (TypeError, ValueError):
            continue
        if parsed not in normalized:
            normalized.append(parsed)
    return tuple(normalized)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    user_ids = tuple(dict.fromkeys(str(value).strip() for value in args.user_id))
    if len(user_ids) < 2:
        raise SystemExit("at least two distinct --user-id values are required")
    if args.tenant_id not in parse_tenant_allowlist(
        settings.agent2_business_tenant_ids
    ):
        raise SystemExit("tenant is not in AGENT2_BUSINESS_TENANT_IDS")
    if args.apply and args.confirm != CONFIRMATION:
        raise SystemExit(f"--confirm must equal {CONFIRMATION}")

    async with AsyncSessionLocal() as session:
        bindings = list(
            (
                await session.scalars(
                    select(Agent2IdentityBinding)
                    .where(
                        Agent2IdentityBinding.tenant_id == args.tenant_id,
                        Agent2IdentityBinding.user_id.in_(user_ids),
                        Agent2IdentityBinding.active.is_(True),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if len(bindings) != len(user_ids):
            raise SystemExit("every requested user must have one active tenant binding")

        before_scopes = {
            item.user_id: _uuid_scope(
                (item.permission_scope_json or {}).get("allowed_case_ids")
            )
            for item in bindings
        }
        shared_case_ids = tuple(
            sorted({case_id for values in before_scopes.values() for case_id in values})
        )
        if not shared_case_ids:
            raise SystemExit("the existing user scopes contain no cases")
        existing_case_ids = {
            str(value)
            for value in (
                await session.scalars(
                    select(Agent2Case.case_id).where(
                        Agent2Case.tenant_id == args.tenant_id,
                        Agent2Case.case_id.in_([UUID(value) for value in shared_case_ids]),
                    )
                )
            ).all()
        }
        if existing_case_ids != set(shared_case_ids):
            raise SystemExit("one or more scoped cases no longer exists in the tenant")

        result_rows: list[dict[str, object]] = []
        now = datetime.now(timezone.utc)
        for binding in bindings:
            before = dict(binding.permission_scope_json or {})
            previous_writable = _uuid_scope(before.get("writable_case_ids"))
            after = {
                **before,
                "allowed_case_ids": list(shared_case_ids),
                "writable_case_ids": list(shared_case_ids),
                "case_progress_collaboration_mode": "explicit_shared_scope",
            }
            changed = before != after
            result_rows.append(
                {
                    "user_id": binding.user_id,
                    "display_name": binding.display_name,
                    "previous_visible_count": len(before_scopes[binding.user_id]),
                    "previous_writable_count": len(previous_writable),
                    "new_visible_count": len(shared_case_ids),
                    "new_writable_count": len(shared_case_ids),
                    "changed": changed,
                }
            )
            if args.apply and changed:
                binding.permission_scope_json = after
                binding.updated_at = now
                session.add(
                    RouteControlAudit(
                        tenant_id=args.tenant_id,
                        actor_user_id=args.actor_user_id,
                        source_message_id=args.source_message_id,
                        before_json={
                            "binding_user_id": binding.user_id,
                            "allowed_case_ids": list(before_scopes[binding.user_id]),
                            "writable_case_ids": list(previous_writable),
                        },
                        after_json={
                            "binding_user_id": binding.user_id,
                            "allowed_case_ids": list(shared_case_ids),
                            "writable_case_ids": list(shared_case_ids),
                        },
                        reason="grant_explicit_case_progress_collaboration",
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
                "shared_case_count": len(shared_case_ids),
                "users": sorted(result_rows, key=lambda item: str(item["display_name"])),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_arguments())))
