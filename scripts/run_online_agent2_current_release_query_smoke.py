from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.memory.postgres import (
    PersonalMemoryAuditRecord,
    PersonalMemoryRecord,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import (
    ToolCallCanaryClearPending,
    ToolCallCanaryReceipt,
)
from app.agent2.tool_calling.salutation_onboarding import (
    ONBOARDING_TOOL_NAME,
    PREFERRED_SALUTATION_KEY,
)
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.legal_daily_roster import load_formal_legal_daily_roster
from app.models import (
    DailyReport,
    ReportInteractionEvent,
    User,
    WebhookEvent,
)

QUERY_TEXT = "给我看一下今天的日报。"


def _private_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _report_state(rows: list[DailyReport]) -> str:
    payload = [
        {
            "id": str(row.id),
            "date": row.report_date.isoformat(),
            "status": row.status,
            "today_work": list(row.today_work or []),
            "problems": list(row.problems or []),
            "tomorrow_plan": list(row.tomorrow_plan or []),
            "section_status": row.section_status or {},
        }
        for row in rows
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _select_formal_agent2_user() -> User:
    settings = get_settings()
    tenant_id = str(
        settings.agent2_weekly_plan_tenant_allowlist or ""
    ).strip()
    legal_tenant_id = str(
        settings.legal_daily_dashboard_tenant_id or ""
    ).strip()
    if not tenant_id or not legal_tenant_id:
        raise RuntimeError("formal Agent2 tenant scope is unavailable")
    async with AsyncSessionLocal() as session:
        roster = await load_formal_legal_daily_roster(
            session,
            tenant_id=legal_tenant_id,
            on_date=datetime.now(ZoneInfo(settings.timezone)).date(),
        )
        formal_user_ids = set(roster.user_ids)
        if len(formal_user_ids) != 74:
            raise RuntimeError("formal Agent2 roster is not exactly 74 users")
        formal_uuids = [UUID(value) for value in formal_user_ids]
        users = list(
            (
                await session.scalars(
                    select(User)
                    .where(
                        User.active.is_(True),
                        User.id.in_(formal_uuids),
                    )
                    .order_by(User.id)
                )
            ).all()
        )
        bindings = list(
            (
                await session.scalars(
                    select(Agent2IdentityBinding).where(
                        Agent2IdentityBinding.tenant_id == tenant_id,
                        Agent2IdentityBinding.active.is_(True)
                    )
                )
            ).all()
        )
        controls = list(
            (
                await session.scalars(
                    select(ToolCallCanaryControl).where(
                        ToolCallCanaryControl.tenant_id == tenant_id,
                        ToolCallCanaryControl.enabled.is_(True),
                        ToolCallCanaryControl.messages_enabled.is_(True),
                        ToolCallCanaryControl.runtime == "canary_execute",
                    )
                )
            ).all()
        )
        salutation_rows = list(
            (
                await session.scalars(
                    select(PersonalMemoryRecord).where(
                        PersonalMemoryRecord.tenant_id == tenant_id,
                        PersonalMemoryRecord.user_id.in_(formal_uuids),
                        PersonalMemoryRecord.memory_key
                        == PREFERRED_SALUTATION_KEY,
                        PersonalMemoryRecord.status == "active",
                    )
                )
            ).all()
        )
        composed_memory_ids = set(
            (
                await session.scalars(
                    select(PersonalMemoryAuditRecord.memory_id).where(
                        PersonalMemoryAuditRecord.tenant_id == tenant_id,
                        PersonalMemoryAuditRecord.memory_id.in_(
                            [row.memory_id for row in salutation_rows]
                        ),
                        PersonalMemoryAuditRecord.action == "compose",
                        PersonalMemoryAuditRecord.tool_name
                        == ONBOARDING_TOOL_NAME,
                    )
                )
            ).all()
        )
    binding_by_user = {row.user_id: row for row in bindings}
    control_scope = {(row.tenant_id, row.user_id) for row in controls}
    salutation_by_user = {
        str(row.user_id): row for row in salutation_rows
    }
    if (
        {str(user.id) for user in users} != formal_user_ids
        or set(binding_by_user) != formal_user_ids
        or {
            user_id
            for scope_tenant, user_id in control_scope
            if scope_tenant == tenant_id
        }
        != formal_user_ids
    ):
        raise RuntimeError(
            "formal roster, identity bindings, and controls differ"
        )
    for user in users:
        binding = binding_by_user.get(str(user.id))
        salutation = salutation_by_user.get(str(user.id))
        onboarding_is_side_effect_free = bool(
            salutation is None
            or salutation.source_kind == "explicit_user"
            or salutation.memory_id in composed_memory_ids
        )
        if (
            binding is not None
            and (binding.tenant_id, str(user.id)) in control_scope
            and user.dingtalk_user_id
            and binding.dingtalk_user_id == user.dingtalk_user_id
            and onboarding_is_side_effect_free
        ):
            return user
    raise RuntimeError("no formal Agent2 identity/control pair is available")


async def _snapshot(user_id: UUID) -> tuple[int, str]:
    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(DailyReport)
                    .where(DailyReport.user_id == user_id)
                    .order_by(DailyReport.report_date, DailyReport.id)
                )
            ).all()
        )
    return len(rows), _report_state(rows)


async def _auxiliary_state(user_id: UUID) -> dict[str, tuple[int, str]]:
    async with AsyncSessionLocal() as session:
        memory_audit_ids = [
            str(value)
            for value in (
                await session.scalars(
                    select(PersonalMemoryAuditRecord.audit_id)
                    .where(PersonalMemoryAuditRecord.user_id == user_id)
                    .order_by(PersonalMemoryAuditRecord.audit_id)
                )
            ).all()
        ]
        interaction_ids = [
            str(value)
            for value in (
                await session.scalars(
                    select(ReportInteractionEvent.id)
                    .where(ReportInteractionEvent.user_id == user_id)
                    .order_by(ReportInteractionEvent.id)
                )
            ).all()
        ]

    def state(values: list[str]) -> tuple[int, str]:
        encoded = ",".join(values).encode("utf-8")
        return len(values), hashlib.sha256(encoded).hexdigest()

    return {
        "personal_memory_audits": state(memory_audit_ids),
        "report_interactions": state(interaction_ids),
    }


async def _cleanup(
    *,
    conversation_id: str,
    source_message_id: str,
) -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ToolCallCanaryClearPending).where(
                ToolCallCanaryClearPending.conversation_id == conversation_id
            )
        )
        await session.execute(
            delete(ToolCallCanaryReceipt).where(
                ToolCallCanaryReceipt.conversation_id == conversation_id
            )
        )
        await session.execute(
            delete(WebhookEvent).where(
                WebhookEvent.idempotency_key == f"manual:{source_message_id}"
            )
        )
        await session.commit()
        residues = {
            "clear_pendings": len(
                list(
                    (
                        await session.scalars(
                            select(ToolCallCanaryClearPending.pending_id).where(
                                ToolCallCanaryClearPending.conversation_id
                                == conversation_id
                            )
                        )
                    ).all()
                )
            ),
            "receipts": len(
                list(
                    (
                        await session.scalars(
                            select(ToolCallCanaryReceipt.receipt_id).where(
                                ToolCallCanaryReceipt.conversation_id
                                == conversation_id
                            )
                        )
                    ).all()
                )
            ),
            "events": len(
                list(
                    (
                        await session.scalars(
                            select(WebhookEvent.id).where(
                                WebhookEvent.idempotency_key
                                == f"manual:{source_message_id}"
                            )
                        )
                    ).all()
                )
            ),
        }
    return residues


async def run(*, base_url: str, output: Path) -> int:
    run_id = uuid4().hex
    conversation_id = f"agent2-current-release-query-smoke:{run_id}"
    source_message_id = f"agent2-current-release-query-smoke-{run_id}"
    user = await _select_formal_agent2_user()
    before_count, before_sha256 = await _snapshot(user.id)
    auxiliary_before = await _auxiliary_state(user.id)
    response_payload: dict[str, Any] = {}
    error_code: str | None = None
    residues = {"receipts": -1, "events": -1}
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(180),
        ) as client:
            health = await client.get("/health")
            if health.status_code != 200:
                raise RuntimeError("health_check_failed")
            response = await client.post(
                "/reports/manual",
                json={
                    "dingtalk_user_id": user.dingtalk_user_id,
                    "raw_input": QUERY_TEXT,
                    "source": "agent2_current_release_query_smoke",
                    "conversation_id": conversation_id,
                    "idempotency_key": source_message_id,
                },
            )
            response.raise_for_status()
            response_payload = response.json()
            if response_payload.get("actual_write") is not False:
                raise RuntimeError("query_reported_a_write")
            if response_payload.get("reply_kind") != "agent2_tool_call_read_only":
                raise RuntimeError("query_did_not_use_tool_call_read_path")
            if int(response_payload.get("model_call_count") or 0) < 1:
                raise RuntimeError("query_did_not_reach_the_model")
    except Exception as exc:  # noqa: BLE001 - output is deliberately sanitized
        value = str(exc)
        error_code = (
            value
            if re.fullmatch(r"[a-z0-9_]{1,80}", value)
            else type(exc).__name__
        )
    finally:
        residues = await _cleanup(
            conversation_id=conversation_id,
            source_message_id=source_message_id,
        )
    after_count, after_sha256 = await _snapshot(user.id)
    auxiliary_after = await _auxiliary_state(user.id)
    passed = bool(
        error_code is None
        and before_count == after_count
        and before_sha256 == after_sha256
        and auxiliary_before == auxiliary_after
        and not any(residues.values())
    )
    payload = {
        "status": "pass" if passed else "failed",
        "base_url": base_url,
        "actual_write": response_payload.get("actual_write"),
        "reply_kind": response_payload.get("reply_kind"),
        "user_visible_result": response_payload.get("user_visible_result"),
        "model_call_count": response_payload.get("model_call_count"),
        "report_state_unchanged": before_sha256 == after_sha256,
        "report_count_unchanged": before_count == after_count,
        "personal_memory_audits_unchanged": (
            auxiliary_before["personal_memory_audits"]
            == auxiliary_after["personal_memory_audits"]
        ),
        "report_interactions_unchanged": (
            auxiliary_before["report_interactions"]
            == auxiliary_after["report_interactions"]
        ),
        "residues": residues,
        "dingtalk_send_calls": 0,
        "identity_fields_included": False,
        "error_code": error_code,
    }
    _private_write(output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    await engine.dispose()
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/agent2_current_release_query_smoke.json"),
    )
    args = parser.parse_args()
    return asyncio.run(run(base_url=args.base_url, output=args.output))


if __name__ == "__main__":
    raise SystemExit(main())
