from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import AsyncSessionLocal, engine
from app.config import get_settings


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect a read-only, value-redacted Agent2 production safety snapshot."
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _scope_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _json_count(value: Any, key: str) -> int:
    if not isinstance(value, dict):
        return 0
    items = value.get(key)
    return len(items) if isinstance(items, list) else 0


def _csv_values(value: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip()
            for item in str(value or "").replace(";", ",").replace("\n", ",").split(",")
            if item.strip()
        )
    )


async def collect_production_safety_snapshot() -> dict[str, Any]:
    settings = get_settings()
    configured_business_tenants = set(
        _csv_values(getattr(settings, "agent2_business_tenant_ids", ""))
    )
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        webhook = (
            await session.execute(
                text(
                    """
                    SELECT
                        count(*) AS total,
                        count(*) FILTER (
                            WHERE external_message_id IS NOT NULL
                              AND external_message_id <> ''
                        ) AS with_external_id,
                        count(*) FILTER (WHERE status = 'processing') AS processing,
                        count(*) FILTER (WHERE status = 'processed') AS processed,
                        count(*) FILTER (WHERE status = 'failed') AS failed
                    FROM webhook_events
                    """
                )
            )
        ).mappings().one()
        duplicate_external = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS duplicate_groups,
                           COALESCE(sum(row_count - 1), 0) AS duplicate_rows
                    FROM (
                        SELECT external_message_id, count(*) AS row_count
                        FROM webhook_events
                        WHERE external_message_id IS NOT NULL
                          AND external_message_id <> ''
                        GROUP BY external_message_id
                        HAVING count(*) > 1
                    ) duplicates
                    """
                )
            )
        ).mappings().one()
        cross_transport = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS duplicate_groups,
                           COALESCE(sum(row_count - 1), 0) AS duplicate_rows
                    FROM (
                        SELECT external_message_id, count(*) AS row_count
                        FROM webhook_events
                        WHERE external_message_id IS NOT NULL
                          AND external_message_id <> ''
                        GROUP BY external_message_id
                        HAVING bool_or(idempotency_key LIKE 'dingtalk:%')
                           AND bool_or(idempotency_key LIKE 'dingtalk-stream:%')
                    ) duplicates
                    """
                )
            )
        ).mappings().one()
        controls = list(
            (
                await session.execute(
                    text(
                        """
                        SELECT tenant_id, route_mode, canary_user_ids,
                               agent1_rollback_enabled, version
                        FROM agent2_tenant_route_controls
                        ORDER BY tenant_id
                        """
                    )
                )
            ).mappings()
        )
        bindings = list(
            (
                await session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, active, permission_scope_json
                        FROM agent2_identity_bindings
                        ORDER BY tenant_id, user_id
                        """
                    )
                )
            ).mappings()
        )
        receipts = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS total,
                           count(*) FILTER (WHERE actual_write) AS actual_writes,
                           count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                           count(*) FILTER (WHERE status = 'failed') AS failed,
                           count(DISTINCT tenant_id) AS tenant_count
                    FROM agent2_business_command_receipts
                    """
                )
            )
        ).mappings().one()
        receipt_status_rows = list(
            (
                await session.execute(
                    text(
                        """
                        SELECT status, count(*) AS row_count
                        FROM agent2_business_command_receipts
                        GROUP BY status
                        ORDER BY status
                        """
                    )
                )
            ).mappings()
        )
        repeated_writes = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS duplicate_groups,
                           COALESCE(sum(row_count - 1), 0) AS duplicate_rows
                    FROM (
                        SELECT tenant_id, source_message_id, command_type,
                               resource_type, resource_id, count(*) AS row_count
                        FROM agent2_business_command_receipts
                        WHERE actual_write
                          AND source_message_id <> ''
                        GROUP BY tenant_id, source_message_id, command_type,
                                 resource_type, resource_id
                        HAVING count(*) > 1
                    ) duplicates
                    """
                )
            )
        ).mappings().one()
        route_audit_count = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM agent2_route_control_audits")
                )
            ).scalar_one()
        )
        reminder_dispatches = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS total,
                           count(*) FILTER (
                               WHERE COALESCE(llm_decision_json->>'provider_reference', '') <> ''
                                 AND llm_decision_json->>'message_status' = 'accepted_by_provider'
                           ) AS provider_evidenced,
                           count(*) FILTER (
                               WHERE COALESCE(llm_decision_json->>'provider_reference', '') = ''
                           ) AS missing_provider_evidence,
                           count(*) FILTER (
                               WHERE COALESCE((llm_decision_json->>'business_write')::boolean, false)
                           ) AS claimed_business_writes
                    FROM report_interaction_events
                    WHERE backend_action = 'daily_report_reminder_sent'
                    """
                )
            )
        ).mappings().one()
        database_identity = (
            await session.execute(
                text(
                    """
                    SELECT current_user AS current_role,
                           current_database() AS database_name,
                           COALESCE(inet_server_addr()::text, 'local') AS server_address,
                           pg_get_userbyid(c.relowner) AS table_owner,
                           current_user = pg_get_userbyid(c.relowner) AS current_role_is_owner,
                           has_schema_privilege(current_user, 'public', 'CREATE') AS can_create_in_public,
                           has_table_privilege(current_user, 'public.webhook_events', 'REFERENCES') AS can_reference_webhook_events
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = 'webhook_events'
                    """
                )
            )
        ).mappings().one()
        claim_table_exists = bool(
            (
                await session.execute(
                    text(
                        "SELECT to_regclass('public.message_ingress_claims') IS NOT NULL"
                    )
                )
            ).scalar_one()
        )
        if claim_table_exists:
            ingress_claims = (
                await session.execute(
                    text(
                        """
                        SELECT count(*) AS total,
                               count(*) FILTER (WHERE event.id IS NULL) AS orphaned,
                               (
                                   SELECT count(*)
                                   FROM webhook_events source
                                   LEFT JOIN message_ingress_claims claim
                                     ON claim.webhook_event_id = source.id
                                    AND claim.idempotency_key = source.idempotency_key
                                   WHERE claim.webhook_event_id IS NULL
                               ) AS unclaimed_webhook_events
                        FROM message_ingress_claims claim
                        LEFT JOIN webhook_events event
                          ON event.id = claim.webhook_event_id
                         AND event.idempotency_key = claim.idempotency_key
                        """
                    )
                )
            ).mappings().one()
        else:
            ingress_claims = {
                "total": 0,
                "orphaned": 0,
                "unclaimed_webhook_events": int(webhook["total"] or 0),
            }
        await session.rollback()

    controls_by_tenant = {
        str(row["tenant_id"]): row for row in controls
    }
    tenant_rows: list[dict[str, Any]] = []
    for tenant_id in sorted(
        set(controls_by_tenant) | {str(row["tenant_id"]) for row in bindings}
    ):
        tenant_bindings = [
            row for row in bindings if str(row["tenant_id"]) == tenant_id
        ]
        control = controls_by_tenant.get(tenant_id)
        tenant_rows.append(
            {
                "tenant_ref": _scope_hash(tenant_id),
                "configured_for_business_entrypoint": tenant_id in configured_business_tenants,
                "route_mode": str(control["route_mode"]) if control else "missing",
                "canary_user_count": len(control["canary_user_ids"] or []) if control else 0,
                "agent1_rollback_enabled": bool(control["agent1_rollback_enabled"]) if control else False,
                "route_version": int(control["version"]) if control else 0,
                "identity_binding_count": len(tenant_bindings),
                "active_binding_count": sum(bool(row["active"]) for row in tenant_bindings),
                "binding_scopes": [
                    {
                        "user_ref": _scope_hash(row["user_id"]),
                        "allowed_case_count": _json_count(
                            row["permission_scope_json"], "allowed_case_ids"
                        ),
                        "writable_case_count": _json_count(
                            row["permission_scope_json"], "writable_case_ids"
                        ),
                    }
                    for row in tenant_bindings
                ],
            }
        )

    return {
        "artifact_version": "agent2.production_safety_snapshot.v1",
        "read_only_transaction": True,
        "webhook_events": {key: int(value or 0) for key, value in webhook.items()},
        "duplicate_external_messages": {
            key: int(value or 0) for key, value in duplicate_external.items()
        },
        "cross_transport_duplicates": {
            key: int(value or 0) for key, value in cross_transport.items()
        },
        "business_receipts": {
            **{key: int(value or 0) for key, value in receipts.items()},
            "by_status": {
                str(row["status"]): int(row["row_count"] or 0)
                for row in receipt_status_rows
            },
        },
        "repeated_business_writes": {
            key: int(value or 0) for key, value in repeated_writes.items()
        },
        "route_control_audit_count": route_audit_count,
        "reminder_dispatches": {
            key: int(value or 0) for key, value in reminder_dispatches.items()
        },
        "database_authority": {
            "current_role_ref": _scope_hash(database_identity["current_role"]),
            "table_owner_ref": _scope_hash(database_identity["table_owner"]),
            "database_ref": _scope_hash(database_identity["database_name"]),
            "server_ref": _scope_hash(database_identity["server_address"]),
            "current_role_is_table_owner": bool(
                database_identity["current_role_is_owner"]
            ),
            "can_create_in_public": bool(database_identity["can_create_in_public"]),
            "can_reference_webhook_events": bool(
                database_identity["can_reference_webhook_events"]
            ),
        },
        "message_ingress_claims": {
            "table_exists": claim_table_exists,
            **{key: int(value or 0) for key, value in ingress_claims.items()},
        },
        "configured_scope": {
            "business_tenant_count": len(configured_business_tenants),
            "business_tenant_refs": sorted(
                _scope_hash(value) for value in configured_business_tenants
            ),
            "semantic_tenant_count": len(
                _csv_values(
                    getattr(settings, "agent2_semantic_admission_tenant_allowlist", "")
                )
            ),
            "semantic_user_count": len(
                _csv_values(
                    getattr(settings, "agent2_semantic_admission_user_allowlist", "")
                )
            ),
            "daily_user_count": len(
                _csv_values(getattr(settings, "agent2_daily_enabled_user_ids", ""))
            ),
            "reminder_user_count": len(
                _csv_values(getattr(settings, "reminder_test_user_ids", ""))
            ),
            "reminder_send_enabled": bool(
                getattr(settings, "reminder_send_enabled", False)
            ),
            "reminder_dry_run": bool(
                getattr(settings, "reminder_dry_run", True)
            ),
            "semantic_admission_enabled": bool(
                getattr(settings, "agent2_semantic_admission_enabled", False)
            ),
            "semantic_admission_enforced": bool(
                getattr(settings, "agent2_semantic_admission_enforce", False)
            ),
            "semantic_admission_review_enabled": bool(
                getattr(settings, "agent2_semantic_admission_review_capture", False)
            ),
            "agent2_case_followup_enabled": bool(
                getattr(settings, "agent2_case_followup_enabled", False)
            ),
            "agent2_case_followup_send_enabled": bool(
                getattr(settings, "agent2_case_followup_send_enabled", False)
            ),
            "case_followup_enabled": bool(
                getattr(settings, "case_followup_enabled", False)
            ),
            "case_followup_send_enabled": bool(
                getattr(settings, "case_followup_send_enabled", False)
            ),
            "case_followup_report_projection_enabled": bool(
                getattr(settings, "case_followup_report_projection_enabled", False)
            ),
        },
        "tenants": tenant_rows,
    }


async def _run() -> int:
    args = _arguments()
    try:
        snapshot = await collect_production_safety_snapshot()
    finally:
        await engine.dispose()
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
